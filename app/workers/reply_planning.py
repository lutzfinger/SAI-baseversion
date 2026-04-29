"""Graph-backed worker for approval-first reply planning."""

from __future__ import annotations

from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_drafts import GmailDraftConnector
from app.control_plane.graph_runtime import GraphRuntimeManager
from app.graphs.reply_planning import (
    ReplyPlanningRuntimeContext,
    ReplyPlanningStateGraphRuntime,
)
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PromptDocument, WorkflowToolDefinition
from app.workers.email_models import EmailClassification, EmailMessage
from app.workers.reply_planning_models import ReplyPlanningItem


class ReplyPlanningWorker:
    """Plan how SAI would answer emails, but write drafts only for approval."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_manager: GraphRuntimeManager,
        gmail_drafts: GmailDraftConnector | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_manager = runtime_manager
        self._gmail_drafts = gmail_drafts

    def plan_replies(
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
        gmail_drafts: GmailDraftConnector | None = None,
    ) -> list[ReplyPlanningItem]:
        runtime_context = ReplyPlanningRuntimeContext(
            operator_email=operator_email,
            prompts_by_tool_id=prompts_by_tool_id,
            tool_definitions=tool_definitions,
        )
        resolved_gmail_drafts = gmail_drafts or self._gmail_drafts or self.build_draft_connector(
            policy=policy
        )
        runtime = ReplyPlanningStateGraphRuntime(
            settings=self.settings,
            runtime_manager=self.runtime_manager,
            gmail_drafts=resolved_gmail_drafts,
        )
        return runtime.process_messages(
            run_id=run_id,
            workflow_id=workflow_id,
            messages=messages,
            email_classifications=email_classifications,
            runtime_context=runtime_context,
        )

    def build_draft_connector(self, *, policy: PolicyDocument) -> GmailDraftConnector:
        return self._gmail_drafts or GmailDraftConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy),
        )
