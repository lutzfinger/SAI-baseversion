"""Read daily token-usage totals from the audit log and LangSmith traces."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from app.observability.tracing_feedback import create_langsmith_client
from app.shared.config import Settings
from app.shared.models import WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.daily_token_usage_report_models import (
    AuditTokenUsageEntry,
    AuditTokenUsageSnapshot,
    LangSmithTokenUsageEntry,
    LangSmithTokenUsageSnapshot,
    TokenUsageTotalLine,
)

_LANGSMITH_MAX_RUN_QUERY_LIMIT = 100


class AuditTokenUsageReaderTool:
    """Read one day of tokenized tool usage from the append-only audit log."""

    def __init__(self, *, tool_definition: WorkflowToolDefinition, settings: Settings) -> None:
        self.tool_definition = tool_definition
        self.settings = settings

    def read_usage(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[AuditTokenUsageSnapshot, ToolExecutionRecord]:
        audit_path = self.settings.audit_log_path
        if not audit_path.exists():
            raise RuntimeError(f"Audit log does not exist yet: {audit_path}")

        entries: list[AuditTokenUsageEntry] = []
        with audit_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                record = json.loads(raw_line)
                if record.get("event_type") != "tool.executed":
                    continue
                timestamp = _parse_timestamp(record.get("timestamp"))
                if timestamp is None or timestamp < started_at or timestamp >= ended_at:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                details = payload.get("details")
                if not isinstance(details, dict):
                    continue
                total_tokens = _extract_total_tokens(details)
                if total_tokens is None or total_tokens <= 0:
                    continue
                workflow_id = str(record.get("workflow_id", "")).strip()
                run_id = str(record.get("run_id", "")).strip()
                tool_id = str(payload.get("tool_id", "")).strip()
                tool_kind = str(payload.get("tool_kind", "")).strip()
                if not workflow_id or not run_id or not tool_id or not tool_kind:
                    continue
                provider = _optional_string(details.get("provider"))
                entries.append(
                    AuditTokenUsageEntry(
                        timestamp=timestamp,
                        run_id=run_id,
                        workflow_id=workflow_id,
                        tool_id=tool_id,
                        tool_kind=tool_kind,
                        total_tokens=total_tokens,
                        provider=provider,
                    )
                )

        workflow_totals = _aggregate_total_lines(
            (entry.workflow_id, entry.total_tokens) for entry in entries
        )
        tool_totals = _aggregate_total_lines(
            (entry.tool_id, entry.total_tokens) for entry in entries
        )
        total_tokens = sum(entry.total_tokens for entry in entries)
        snapshot = AuditTokenUsageSnapshot(
            status="actual",
            source="audit_jsonl",
            started_at=started_at,
            ended_at=ended_at,
            entries=entries,
            workflow_totals=workflow_totals,
            tool_totals=tool_totals,
            total_tokens=total_tokens,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "audit_log_path": str(audit_path),
                "entry_count": len(entries),
                "workflow_count": len(workflow_totals),
                "tool_count": len(tool_totals),
                "total_tokens": total_tokens,
            },
        )
        return snapshot, record


class LangSmithTokenUsageReaderTool:
    """Read one day of LangSmith root-run token usage for audit-gap comparison."""

    def __init__(self, *, tool_definition: WorkflowToolDefinition, settings: Settings) -> None:
        self.tool_definition = tool_definition
        self.settings = settings
        self.max_root_runs = _resolve_max_root_runs(tool_definition, default=100)

    def read_usage(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[LangSmithTokenUsageSnapshot, ToolExecutionRecord]:
        client = create_langsmith_client(self.settings)
        selected_roots = list(
            client.list_runs(
                project_name=self.settings.langsmith_project,
                is_root=True,
                start_time=started_at,
                limit=self.max_root_runs,
            )
        )
        entries: list[LangSmithTokenUsageEntry] = []
        root_run_count = 0
        for root_run in selected_roots:
            root_started_at = _coerce_datetime(getattr(root_run, "start_time", None))
            if root_started_at is None or root_started_at >= ended_at:
                continue
            root_run_count += 1
            workflow_id = _workflow_id_from_run(root_run)
            root_run_id = str(getattr(root_run, "id", "")).strip()
            total_tokens = _int_or_none(getattr(root_run, "total_tokens", None))
            if not root_run_id or total_tokens is None or total_tokens <= 0:
                continue
            node_name = str(getattr(root_run, "name", "")).strip() or "unnamed_run"
            entries.append(
                LangSmithTokenUsageEntry(
                    run_id=root_run_id,
                    workflow_id=workflow_id,
                    node_name=node_name,
                    path=node_name,
                    total_tokens=total_tokens,
                    total_cost=_float_or_none(getattr(root_run, "total_cost", None)),
                )
            )

        total_tokens = sum(entry.total_tokens for entry in entries)
        snapshot = LangSmithTokenUsageSnapshot(
            status="actual",
            source="langsmith",
            started_at=started_at,
            ended_at=ended_at,
            entries=entries,
            total_tokens=total_tokens,
            root_run_count=root_run_count,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "project_name": self.settings.langsmith_project,
                "max_root_runs": self.max_root_runs,
                "root_run_count": root_run_count,
                "entry_count": len(entries),
                "total_tokens": total_tokens,
            },
        )
        return snapshot, record


def compute_langsmith_only_totals(
    *,
    audit_entries: list[AuditTokenUsageEntry],
    langsmith_entries: list[LangSmithTokenUsageEntry],
) -> list[TokenUsageTotalLine]:
    audited_run_ids = {entry.run_id for entry in audit_entries}
    totals: dict[str, int] = defaultdict(int)
    for entry in langsmith_entries:
        if entry.run_id in audited_run_ids:
            continue
        label = (entry.workflow_id or "").strip() or entry.node_name
        totals[label] += entry.total_tokens
    return _aggregate_total_lines(totals.items())


def _aggregate_total_lines(items: Iterable[tuple[str, int]]) -> list[TokenUsageTotalLine]:
    totals: dict[str, int] = defaultdict(int)
    for label, token_count in items:
        clean_label = str(label).strip()
        if not clean_label:
            continue
        totals[clean_label] += int(token_count)
    return [
        TokenUsageTotalLine(label=label, token_count=token_count)
        for label, token_count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def _resolve_max_root_runs(tool_definition: WorkflowToolDefinition, *, default: int) -> int:
    raw_value = tool_definition.config.get("max_root_runs", default)
    try:
        max_root_runs = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Tool {tool_definition.tool_id} max_root_runs must be an integer."
        ) from error
    if max_root_runs <= 0:
        raise ValueError(f"Tool {tool_definition.tool_id} max_root_runs must be positive.")
    return min(max_root_runs, _LANGSMITH_MAX_RUN_QUERY_LIMIT)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _extract_total_tokens(details: dict[str, Any]) -> int | None:
    direct_total = _int_or_none(details.get("total_tokens"))
    if direct_total is not None and direct_total > 0:
        return direct_total
    usage = details.get("usage")
    if isinstance(usage, dict):
        nested_total = _int_or_none(usage.get("total_tokens"))
        if nested_total is not None and nested_total > 0:
            return nested_total
        input_tokens = _int_or_none(usage.get("input_tokens")) or 0
        output_tokens = _int_or_none(usage.get("output_tokens")) or 0
        if input_tokens or output_tokens:
            return input_tokens + output_tokens
    input_tokens = _int_or_none(details.get("input_tokens")) or 0
    output_tokens = _int_or_none(details.get("output_tokens")) or 0
    if input_tokens or output_tokens:
        return input_tokens + output_tokens
    return None


def _workflow_id_from_run(run: Any) -> str | None:
    extra = getattr(run, "extra", None)
    if not isinstance(extra, dict):
        return None
    metadata = extra.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("workflow_id")
        if value:
            return str(value)
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return None


def _int_or_none(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(cast(int | str, value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(cast(float | str, value))
    except (TypeError, ValueError):
        return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
