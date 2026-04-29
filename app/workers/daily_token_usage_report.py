"""Worker for one daily token-usage report sent to Slack."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.slack import SlackPostConnector
from app.shared.config import Settings
from app.shared.models import PolicyDocument, WorkflowToolDefinition
from app.shared.tool_registry import get_tool_spec
from app.tools.daily_token_usage import (
    AuditTokenUsageReaderTool,
    LangSmithTokenUsageReaderTool,
    compute_langsmith_only_totals,
)
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.daily_token_usage_report_models import (
    AuditTokenUsageSnapshot,
    DailyTokenUsageReportResult,
    DailyTokenUsageSlackDelivery,
    LangSmithTokenUsageSnapshot,
    TokenUsageTotalLine,
)


class DailyTokenUsageReportWorker:
    """Fetch yesterday's token usage and post one Slack summary."""

    def __init__(
        self,
        *,
        settings: Settings,
        slack_connector: SlackPostConnector | None = None,
    ) -> None:
        self.settings = settings
        self._slack_connector = slack_connector

    def build_slack_connector(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> SlackPostConnector:
        tool = _tool_or_raise(tool_definitions, "slack_message_sender")
        channel_name = (
            str(tool.config.get("channel_name", "")).strip()
            or self.settings.slack_cost_channel
        )
        return self._slack_connector or SlackPostConnector(
            policy=policy,
            default_channel=channel_name,
        )

    def required_actions(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> list[ConnectorAction]:
        slack = self.build_slack_connector(policy=policy, tool_definitions=tool_definitions)
        actions = [
            *slack.required_actions(),
            *_tool_required_actions("audit_token_usage_reader"),
            *_tool_required_actions("langsmith_token_usage_reader"),
        ]
        return _dedupe_actions(actions)

    def connector_descriptors(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> list[ConnectorDescriptor]:
        slack = self.build_slack_connector(policy=policy, tool_definitions=tool_definitions)
        return [slack.describe()]

    def create_report(
        self,
        *,
        tool_definitions: list[WorkflowToolDefinition],
        policy: PolicyDocument,
        slack_connector: SlackPostConnector | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> DailyTokenUsageReportResult:
        audit_definition = _tool_or_raise(tool_definitions, "audit_token_usage_reader")
        langsmith_definition = _tool_or_raise(tool_definitions, "langsmith_token_usage_reader")
        slack = slack_connector or self.build_slack_connector(
            policy=policy,
            tool_definitions=tool_definitions,
        )
        (
            started_at,
            ended_at,
            window_label,
            window_display_label,
            timezone_name,
            timezone_abbreviation,
        ) = _previous_local_day_window()
        tool_records: list[ToolExecutionRecord] = []
        _report_progress(
            progress_callback,
            "daily_token_usage_report.window",
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            window_label=window_label,
            timezone_name=timezone_name,
        )

        audit_snapshot = _read_audit_snapshot(
            settings=self.settings,
            progress_callback=progress_callback,
            tool_definition=audit_definition,
            tool_records=tool_records,
            started_at=started_at,
            ended_at=ended_at,
        )
        langsmith_snapshot = _read_langsmith_snapshot(
            settings=self.settings,
            progress_callback=progress_callback,
            tool_definition=langsmith_definition,
            tool_records=tool_records,
            started_at=started_at,
            ended_at=ended_at,
        )
        langsmith_only_totals = compute_langsmith_only_totals(
            audit_entries=audit_snapshot.entries,
            langsmith_entries=langsmith_snapshot.entries,
        )
        message = _build_slack_message(
            window_display_label=window_display_label,
            timezone_abbreviation=timezone_abbreviation,
            audit_snapshot=audit_snapshot,
            langsmith_snapshot=langsmith_snapshot,
            langsmith_only_totals=langsmith_only_totals,
        )
        post_result = slack.post_message(text=message)
        slack_delivery = DailyTokenUsageSlackDelivery(
            channel=post_result["channel"],
            text=message,
            posted=True,
            ts=post_result.get("ts"),
        )
        tool_records.append(
            ToolExecutionRecord(
                tool_id="daily_token_usage_report_slack_sender",
                tool_kind="slack_message_sender",
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "channel": slack_delivery.channel,
                    "ts": slack_delivery.ts,
                    "text_chars": len(message),
                },
            )
        )
        _report_progress(
            progress_callback,
            "daily_token_usage_report.completed",
            audit_status=audit_snapshot.status,
            langsmith_status=langsmith_snapshot.status,
            slack_channel=slack_delivery.channel,
        )
        return DailyTokenUsageReportResult(
            window_label=window_label,
            window_display_label=window_display_label,
            timezone_name=timezone_name,
            timezone_abbreviation=timezone_abbreviation,
            audit=audit_snapshot,
            langsmith=langsmith_snapshot,
            langsmith_only_totals=langsmith_only_totals,
            slack=slack_delivery,
            tool_records=tool_records,
        )


def _tool_or_raise(
    tool_definitions: list[WorkflowToolDefinition],
    kind: str,
) -> WorkflowToolDefinition:
    for definition in tool_definitions:
        if definition.enabled and definition.kind == kind:
            return definition
    raise ValueError(f"Workflow is missing required tool kind: {kind}")


def _tool_required_actions(kind: str) -> list[ConnectorAction]:
    spec = get_tool_spec(kind)
    return [
        ConnectorAction(action=action, reason=spec.purpose)
        for action in spec.required_actions
    ]


def _dedupe_actions(actions: list[ConnectorAction]) -> list[ConnectorAction]:
    deduped: dict[str, ConnectorAction] = {}
    for action in actions:
        deduped.setdefault(action.action, action)
    return list(deduped.values())


def _previous_local_day_window() -> tuple[datetime, datetime, str, str, str, str]:
    now_local = datetime.now().astimezone()
    tzinfo = now_local.tzinfo
    assert tzinfo is not None
    yesterday = now_local.date() - timedelta(days=1)
    started_local = datetime.combine(yesterday, time.min, tzinfo=tzinfo)
    ended_local = started_local + timedelta(days=1)
    return (
        started_local.astimezone(UTC),
        ended_local.astimezone(UTC),
        yesterday.isoformat(),
        started_local.strftime("%B %-d, %Y"),
        str(getattr(tzinfo, "key", None) or now_local.tzname() or "local"),
        str(started_local.tzname() or now_local.tzname() or "local"),
    )


def _read_audit_snapshot(
    *,
    settings: Settings,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    tool_definition: WorkflowToolDefinition,
    tool_records: list[ToolExecutionRecord],
    started_at: datetime,
    ended_at: datetime,
) -> AuditTokenUsageSnapshot:
    _report_progress(progress_callback, "daily_token_usage_report.audit.start")
    try:
        snapshot, record = AuditTokenUsageReaderTool(
            tool_definition=tool_definition,
            settings=settings,
        ).read_usage(started_at=started_at, ended_at=ended_at)
    except RuntimeError as error:
        snapshot = AuditTokenUsageSnapshot(
            status="unavailable",
            source="audit_jsonl",
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.SKIPPED,
            details={"reason": str(error)},
        )
    except Exception as error:
        snapshot = AuditTokenUsageSnapshot(
            status="failed",
            source="audit_jsonl",
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.FAILED,
            details={"error": str(error)},
        )
    tool_records.append(record)
    _report_progress(
        progress_callback,
        "daily_token_usage_report.audit.completed",
        status=snapshot.status,
        total_tokens=snapshot.total_tokens,
        entry_count=len(snapshot.entries),
    )
    return snapshot


def _read_langsmith_snapshot(
    *,
    settings: Settings,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    tool_definition: WorkflowToolDefinition,
    tool_records: list[ToolExecutionRecord],
    started_at: datetime,
    ended_at: datetime,
) -> LangSmithTokenUsageSnapshot:
    _report_progress(progress_callback, "daily_token_usage_report.langsmith.start")
    try:
        snapshot, record = LangSmithTokenUsageReaderTool(
            tool_definition=tool_definition,
            settings=settings,
        ).read_usage(started_at=started_at, ended_at=ended_at)
    except RuntimeError as error:
        snapshot = LangSmithTokenUsageSnapshot(
            status="unavailable",
            source="langsmith",
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.SKIPPED,
            details={"reason": str(error)},
        )
    except Exception as error:
        snapshot = LangSmithTokenUsageSnapshot(
            status="failed",
            source="langsmith",
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_definition.tool_id,
            tool_kind=tool_definition.kind,
            status=ToolExecutionStatus.FAILED,
            details={"error": str(error)},
        )
    tool_records.append(record)
    _report_progress(
        progress_callback,
        "daily_token_usage_report.langsmith.completed",
        status=snapshot.status,
        total_tokens=snapshot.total_tokens,
        entry_count=len(snapshot.entries),
    )
    return snapshot


def _build_slack_message(
    *,
    window_display_label: str,
    timezone_abbreviation: str,
    audit_snapshot: AuditTokenUsageSnapshot,
    langsmith_snapshot: LangSmithTokenUsageSnapshot,
    langsmith_only_totals: list[TokenUsageTotalLine],
) -> str:
    lines = [f"On {window_display_label} ({timezone_abbreviation})"]
    if audit_snapshot.status == "actual":
        if audit_snapshot.workflow_totals:
            lines.append("Top workflows by audit tokens:")
            for line in audit_snapshot.workflow_totals[:3]:
                lines.append(f"{line.label}: {_format_tokens(line.token_count)} tokens")
        if audit_snapshot.tool_totals:
            lines.append("Top tools by audit tokens:")
            for line in audit_snapshot.tool_totals[:4]:
                lines.append(f"{line.label}: {_format_tokens(line.token_count)} audit tokens")
        if (
            isinstance(audit_snapshot.total_tokens, int)
            and audit_snapshot.total_tokens > 0
            and not audit_snapshot.workflow_totals
            and not audit_snapshot.tool_totals
        ):
            lines.append(f"Audit total: {_format_tokens(audit_snapshot.total_tokens)} tokens")
    else:
        note = (audit_snapshot.note or "unknown audit error").strip()
        lines.append(f"Audit token summary unavailable: {note}")

    if langsmith_only_totals:
        lines.append("LangSmith-only tokens:")
        for line in langsmith_only_totals[:3]:
            lines.append(f"{line.label}: {_format_tokens(line.token_count)} LangSmith-only tokens")
    elif langsmith_snapshot.status == "actual":
        lines.append("LangSmith-only tokens: none")
    else:
        note = (langsmith_snapshot.note or "LangSmith not available").strip()
        lines.append(f"LangSmith supplement unavailable: {note}")
    return "\n".join(lines)


def _format_tokens(value: int) -> str:
    return f"{value:,}"


def _report_progress(
    callback: Callable[[str, dict[str, Any]], None] | None,
    stage: str,
    **details: Any,
) -> None:
    if callback is None:
        return
    callback(stage, details)
