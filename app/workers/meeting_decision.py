"""Graph-backed worker for the Phase 1 meeting decision workflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.calendar import CalendarHistoryConnector
from app.connectors.calendar_auth import CalendarOAuthAuthenticator
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_config import GmailConnectorPolicy
from app.connectors.gmail_history import parse_extra_gmail_token_paths
from app.connectors.gmail_history import GmailHistoryConnector
from app.connectors.linkedin_config import LinkedInDatasetPolicy
from app.connectors.linkedin_dataset import LinkedInDatasetConnector
from app.control_plane.graph_runtime import GraphRuntimeManager
from app.graphs.meeting_decision import (
    MeetingDecisionRuntimeContext,
    MeetingDecisionStateGraphRuntime,
)
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PromptDocument, WorkflowToolDefinition
from app.workers.email_models import EmailClassification, EmailMessage
from app.workers.meeting_models import MeetingDecisionItem


class MeetingDecisionWorker:
    """Small scoped worker that enriches and drafts meeting decisions."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_manager: GraphRuntimeManager,
        gmail_history: GmailHistoryConnector | None = None,
        calendar_history: CalendarHistoryConnector | None = None,
        linkedin_lookup: LinkedInDatasetConnector | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_manager = runtime_manager
        self._gmail_history = gmail_history
        self._calendar_history = calendar_history
        self._linkedin_lookup = linkedin_lookup

    def decide_meetings(
        self,
        *,
        run_id: str,
        workflow_id: str,
        messages: list[EmailMessage],
        email_classifications: list[EmailClassification],
        prompts_by_tool_id: dict[str, PromptDocument],
        tool_definitions: list[WorkflowToolDefinition],
        policy: PolicyDocument,
        operator_email: str,
        gmail_history: GmailHistoryConnector | None = None,
        calendar_history: CalendarHistoryConnector | None = None,
        linkedin_lookup: LinkedInDatasetConnector | None = None,
    ) -> list[MeetingDecisionItem]:
        built_gmail_history: GmailHistoryConnector | None = None
        built_calendar_history: CalendarHistoryConnector | None = None
        built_linkedin_lookup: LinkedInDatasetConnector | None = None
        if gmail_history is None or calendar_history is None or linkedin_lookup is None:
            (
                built_gmail_history,
                built_calendar_history,
                built_linkedin_lookup,
            ) = self.build_connectors(policy=policy, tool_definitions=tool_definitions)
        runtime_context = MeetingDecisionRuntimeContext(
            operator_email=operator_email,
            calendar_link=self.settings.meeting_calendar_link,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
        )
        resolved_gmail_history = gmail_history or self._gmail_history or built_gmail_history
        resolved_calendar_history = (
            calendar_history or self._calendar_history or built_calendar_history
        )
        resolved_linkedin_lookup = (
            linkedin_lookup or self._linkedin_lookup or built_linkedin_lookup
        )
        assert resolved_gmail_history is not None
        assert resolved_calendar_history is not None
        assert resolved_linkedin_lookup is not None
        runtime = MeetingDecisionStateGraphRuntime(
            settings=self.settings,
            runtime_manager=self.runtime_manager,
            gmail_history=resolved_gmail_history,
            calendar_history=resolved_calendar_history,
            linkedin_lookup=resolved_linkedin_lookup,
        )
        return runtime.process_candidates(
            run_id=run_id,
            workflow_id=workflow_id,
            messages=messages,
            email_classifications=email_classifications,
            runtime_context=runtime_context,
        )

    def build_connectors(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> tuple[GmailHistoryConnector, CalendarHistoryConnector, LinkedInDatasetConnector]:
        gmail_history = self._gmail_history or self._build_gmail_history(policy, tool_definitions)
        return (
            gmail_history,
            self._calendar_history
            or self._build_calendar_history(
                policy,
                tool_definitions,
                gmail_history=gmail_history,
            ),
            self._linkedin_lookup or self._build_linkedin_lookup(policy),
        )

    def required_actions(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> list[ConnectorAction]:
        gmail_history, calendar_history, linkedin_lookup = self.build_connectors(
            policy=policy,
            tool_definitions=tool_definitions,
        )
        return [
            *gmail_history.required_actions(),
            *calendar_history.required_actions(),
            *linkedin_lookup.required_actions(),
        ]

    def connector_descriptors(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> list[ConnectorDescriptor]:
        gmail_history, calendar_history, linkedin_lookup = self.build_connectors(
            policy=policy,
            tool_definitions=tool_definitions,
        )
        return [
            gmail_history.describe(),
            calendar_history.describe(),
            linkedin_lookup.describe(),
        ]

    def authenticate_connectors(
        self,
        *,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
        open_browser: bool = True,
    ) -> dict[str, Any]:
        del tool_definitions
        gmail_auth = GmailOAuthAuthenticator(settings=self.settings, policy=policy)
        calendar_auth = CalendarOAuthAuthenticator(settings=self.settings, policy=policy)
        gmail_token = gmail_auth.authenticate_interactively(open_browser=open_browser)
        calendar_token = calendar_auth.authenticate_interactively(open_browser=open_browser)
        return {
            "gmail_token_path": str(gmail_token),
            "gmail_scopes": gmail_auth.gmail_policy.allowed_scopes,
            "calendar_token_path": str(calendar_token),
            "calendar_scopes": calendar_auth.calendar_policy.allowed_scopes,
        }

    def _build_gmail_history(
        self,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
    ) -> GmailHistoryConnector:
        tool = _tool_or_raise(tool_definitions, "gmail_history_enrichment")
        config = tool.config
        gmail_policy = GmailConnectorPolicy.from_policy(policy)
        extra_authenticators: list[GmailOAuthAuthenticator] = []
        extra_token_paths_env = gmail_policy.extra_token_paths_env
        extra_token_paths = parse_extra_gmail_token_paths(
            os.getenv(extra_token_paths_env) if extra_token_paths_env else None
        )
        for token_path in extra_token_paths:
            if not token_path.exists():
                continue
            extra_authenticators.append(
                GmailOAuthAuthenticator(
                    settings=self.settings,
                    policy=policy,
                    token_path_override=token_path,
                )
            )
        return GmailHistoryConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy),
            extra_authenticators=extra_authenticators,
            max_history_results=int(config.get("max_history_results", 25)),
        )

    def _build_calendar_history(
        self,
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
        *,
        gmail_history: GmailHistoryConnector | None = None,
    ) -> CalendarHistoryConnector:
        tool = _tool_or_raise(tool_definitions, "calendar_history_enrichment")
        config = tool.config
        return CalendarHistoryConnector(
            authenticator=CalendarOAuthAuthenticator(settings=self.settings, policy=policy),
            calendar_id=str(config.get("calendar_id", "primary")),
            lookback_days=int(
                config.get(
                    "lookback_days",
                    self.settings.meeting_history_lookback_days,
                )
            ),
            max_results=int(config.get("max_results", 250)),
            search_all_calendars=bool(config.get("search_all_calendars", False)),
            gmail_history=gmail_history,
        )

    def _build_linkedin_lookup(self, policy: PolicyDocument) -> LinkedInDatasetConnector:
        linkedin_policy = LinkedInDatasetPolicy.from_policy(policy)
        raw_path = os.getenv(linkedin_policy.dataset_path_env)
        dataset_path: Path | None
        if raw_path:
            dataset_path = Path(raw_path).expanduser()
        else:
            dataset_path = self.settings.linkedin_dataset_path
        return LinkedInDatasetConnector(dataset_path=dataset_path)


def _tool_or_raise(
    tool_definitions: list[WorkflowToolDefinition],
    kind: str,
) -> WorkflowToolDefinition:
    for tool in tool_definitions:
        if tool.kind == kind and tool.enabled:
            return tool
    raise KeyError(f"Workflow is missing required tool kind: {kind}")
