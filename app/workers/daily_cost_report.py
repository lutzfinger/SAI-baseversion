"""Worker for one daily provider cost report sent to Slack."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.slack import SlackPostConnector
from app.shared.config import Settings
from app.shared.models import PolicyDocument, WorkflowToolDefinition
from app.shared.tool_registry import get_tool_spec
from app.tools.daily_costs import GeminiDailyCostReaderTool, OpenAIDailyCostReaderTool
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.daily_cost_report_models import (
    DailyCostReportResult,
    DailyCostSlackDelivery,
    DailyProviderCost,
)


class DailyCostReportWorker:
    """Fetch provider costs for yesterday and post one Slack summary."""

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
            *_tool_required_actions("openai_cost_reader"),
            *_tool_required_actions("gemini_cost_reader"),
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
    ) -> DailyCostReportResult:
        openai_definition = _tool_or_raise(tool_definitions, "openai_cost_reader")
        gemini_definition = _tool_or_raise(tool_definitions, "gemini_cost_reader")
        slack = slack_connector or self.build_slack_connector(
            policy=policy,
            tool_definitions=tool_definitions,
        )
        started_at, ended_at, window_label, timezone_name = _previous_local_day_window()
        tool_records: list[ToolExecutionRecord] = []
        _report_progress(
            progress_callback,
            "daily_cost_report.window",
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            window_label=window_label,
            timezone_name=timezone_name,
        )
        openai_cost = _read_provider_cost(
            progress_callback=progress_callback,
            stage_prefix="daily_cost_report.openai",
            reader=lambda: OpenAIDailyCostReaderTool(
                tool_definition=openai_definition,
                settings=self.settings,
            ).read_cost(started_at=started_at, ended_at=ended_at),
            tool_id=openai_definition.tool_id,
            tool_kind=openai_definition.kind,
            provider="openai",
            started_at=started_at,
            ended_at=ended_at,
            source="openai_cost_api",
            tool_records=tool_records,
        )
        gemini_cost = _read_provider_cost(
            progress_callback=progress_callback,
            stage_prefix="daily_cost_report.gemini",
            reader=lambda: GeminiDailyCostReaderTool(
                tool_definition=gemini_definition,
                settings=self.settings,
            ).read_cost(started_at=started_at, ended_at=ended_at),
            tool_id=gemini_definition.tool_id,
            tool_kind=gemini_definition.kind,
            provider="gemini",
            started_at=started_at,
            ended_at=ended_at,
            source="google_cloud_billing_export",
            tool_records=tool_records,
        )
        message = _build_slack_message(
            window_label=window_label,
            timezone_name=timezone_name,
            openai=openai_cost,
            gemini=gemini_cost,
        )
        post_result = slack.post_message(text=message)
        slack_delivery = DailyCostSlackDelivery(
            channel=post_result["channel"],
            text=message,
            posted=True,
            ts=post_result.get("ts"),
        )
        tool_records.append(
            ToolExecutionRecord(
                tool_id="daily_cost_report_slack_sender",
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
            "daily_cost_report.completed",
            openai_status=openai_cost.status,
            gemini_status=gemini_cost.status,
            slack_channel=slack_delivery.channel,
        )
        return DailyCostReportResult(
            window_label=window_label,
            timezone_name=timezone_name,
            openai=openai_cost,
            gemini=gemini_cost,
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


def _previous_local_day_window() -> tuple[datetime, datetime, str, str]:
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
        str(getattr(tzinfo, "key", None) or now_local.tzname() or "local"),
    )


def _read_provider_cost(
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    stage_prefix: str,
    reader: Callable[[], tuple[DailyProviderCost, ToolExecutionRecord]],
    tool_id: str,
    tool_kind: str,
    provider: str,
    started_at: datetime,
    ended_at: datetime,
    source: str,
    tool_records: list[ToolExecutionRecord],
) -> DailyProviderCost:
    _report_progress(progress_callback, f"{stage_prefix}.start")
    try:
        cost, record = reader()
    except RuntimeError as error:
        cost = DailyProviderCost(
            provider=provider,  # type: ignore[arg-type]
            status="unavailable",
            source=source,
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_id,
            tool_kind=tool_kind,
            status=ToolExecutionStatus.SKIPPED,
            details={"reason": str(error)},
        )
    except Exception as error:
        cost = DailyProviderCost(
            provider=provider,  # type: ignore[arg-type]
            status="failed",
            source=source,
            started_at=started_at,
            ended_at=ended_at,
            note=str(error),
        )
        record = ToolExecutionRecord(
            tool_id=tool_id,
            tool_kind=tool_kind,
            status=ToolExecutionStatus.FAILED,
            details={"error": str(error)},
        )
    tool_records.append(record)
    _report_progress(
        progress_callback,
        f"{stage_prefix}.completed",
        status=cost.status,
        amount_usd=cost.amount_usd,
        note=cost.note,
    )
    return cost


def _build_slack_message(
    *,
    window_label: str,
    timezone_name: str,
    openai: DailyProviderCost,
    gemini: DailyProviderCost,
) -> str:
    lines = [
        f"SAI daily cost report for `{window_label}` ({timezone_name})",
        f"- OpenAI: {_format_provider_cost(openai)}",
        f"- Gemini: {_format_provider_cost(gemini)}",
    ]
    totals = [
        cost.amount_usd
        for cost in (openai, gemini)
        if cost.status == "actual" and cost.amount_usd is not None
    ]
    if totals:
        lines.append(f"- Total reported: `${sum(totals):.6f}`")
    return "\n".join(lines)


def _format_provider_cost(cost: DailyProviderCost) -> str:
    if cost.status == "actual" and cost.amount_usd is not None:
        return f"`${cost.amount_usd:.6f}` via {cost.source}"
    note = cost.note or "not available"
    return f"{cost.status} — {note}"


def _report_progress(
    callback: Callable[[str, dict[str, Any]], None] | None,
    stage: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(stage, details)
