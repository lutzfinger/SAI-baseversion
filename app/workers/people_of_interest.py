"""Weekly people-of-interest monitoring and Slack delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.slack import SlackPostConnector
from app.learning.people_of_interest_registry import (
    load_people_of_interest_registry,
)
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PromptDocument, WorkflowToolDefinition
from app.shared.tool_registry import get_tool_spec
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.people_of_interest_research import PeopleOfInterestResearchTool
from app.workers.people_of_interest_models import (
    PeopleOfInterestReviewResult,
    PersonOfInterestReviewItem,
    PersonOfInterestSearchResponse,
)


class PeopleOfInterestWorker:
    """Run weekly OpenAI searches for a curated people-of-interest list."""

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
            or self.settings.slack_people_of_interest_channel
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
            *_tool_required_actions("people_of_interest_researcher"),
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

    def review_people(
        self,
        *,
        policy: PolicyDocument,
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        research_tool: PeopleOfInterestResearchTool | Any | None = None,
        slack_connector: SlackPostConnector | None = None,
    ) -> PeopleOfInterestReviewResult:
        registry = load_people_of_interest_registry(self.settings.people_of_interest_registry_path)
        research_definition = _tool_or_raise(tool_definitions, "people_of_interest_researcher")
        resolved_research_tool = research_tool or PeopleOfInterestResearchTool(
            tool_definition=research_definition,
            prompt=prompts_by_tool_id[research_definition.tool_id],
            settings=self.settings,
        )
        slack = slack_connector or self.build_slack_connector(
            policy=policy,
            tool_definitions=tool_definitions,
        )

        items: list[PersonOfInterestReviewItem] = []
        tool_records: list[ToolExecutionRecord] = []
        searched_count = 0
        updated_count = 0
        failed_count = 0

        for person in registry.people:
            try:
                report, record = resolved_research_tool.research(person=person)
            except Exception as error:
                failed_count += 1
                failure_record = ToolExecutionRecord(
                    tool_id=research_definition.tool_id,
                    tool_kind=research_definition.kind,
                    status=ToolExecutionStatus.FAILED,
                    details={
                        "person_id": person.person_id,
                        "display_name": person.display_name,
                        "error": str(error),
                    },
                )
                tool_records.append(failure_record)
                items.append(
                    PersonOfInterestReviewItem(
                        person_id=person.person_id,
                        display_name=person.display_name,
                        canonical_url=person.canonical_url,
                        organization=person.organization,
                        status="failed",
                        tool_records=[failure_record],
                    )
                )
                continue

            searched_count += 1
            if not report.no_notable_updates:
                updated_count += 1
            items.append(
                PersonOfInterestReviewItem(
                    person_id=person.person_id,
                    display_name=person.display_name,
                    canonical_url=person.canonical_url,
                    organization=person.organization,
                    status="updated" if not report.no_notable_updates else "no_updates",
                    report=report,
                    tool_records=[record],
                )
            )
            tool_records.append(record)

        message = _build_people_summary_text(items=items)
        post_result = slack.post_message(text=message)
        slack_record = ToolExecutionRecord(
            tool_id="people_of_interest_slack_sender",
            tool_kind="slack_message_sender",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "channel": post_result["channel"],
                "ts": post_result.get("ts"),
                "people_count": len(registry.people),
                "searched_count": searched_count,
                "updated_count": updated_count,
                "failed_count": failed_count,
            },
        )
        tool_records.append(slack_record)

        return PeopleOfInterestReviewResult(
            people_count=len(registry.people),
            searched_count=searched_count,
            updated_count=updated_count,
            failed_count=failed_count,
            slack_channel=post_result["channel"],
            slack_ts=post_result.get("ts"),
            items=items,
            tool_records=tool_records,
        )


def _build_people_summary_text(*, items: list[PersonOfInterestReviewItem]) -> str:
    now = datetime.now().astimezone()
    week_ago = now - timedelta(days=7)
    lines = [
        "Weekly people-of-interest update",
        f"Window: {week_ago.date().isoformat()} to {now.date().isoformat()}",
        "",
    ]
    for item in items:
        lines.append(_build_person_summary_line(item=item))
    return "\n".join(lines)


def _build_person_summary_line(*, item: PersonOfInterestReviewItem) -> str:
    prefix = f"- {item.display_name}: "
    if item.status == "failed":
        return _truncate_with_ellipsis(prefix + "search failed this week.", max_chars=300)
    report = item.report or PersonOfInterestSearchResponse(
        search_query=item.display_name,
        overall_summary="No report available.",
        no_notable_updates=True,
    )
    if report.no_notable_updates:
        return _truncate_with_ellipsis(
            prefix + "no notable public updates found.",
            max_chars=300,
        )
    return _truncate_with_ellipsis(prefix + report.overall_summary, max_chars=300)


def _truncate_with_ellipsis(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    truncated = text[: max_chars - 3].rstrip()
    return f"{truncated}..."


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
    return [ConnectorAction(action=action, reason=spec.purpose) for action in spec.required_actions]


def _dedupe_actions(actions: list[ConnectorAction]) -> list[ConnectorAction]:
    deduped: dict[str, ConnectorAction] = {}
    for action in actions:
        deduped.setdefault(action.action, action)
    return list(deduped.values())
