"""Worker helpers for starter email-native workflow suggestions."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_documents import GmailDocumentConnector
from app.connectors.gmail_send import GmailSendConnector
from app.learning.sai_email_dataset import (
    SaiEmailActivityRecord,
    SaiEmailActivityStore,
    SaiEmailGoldenDatasetStore,
    SaiEmailGoldenRecord,
    utc_now,
)
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PromptDocument, WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord
from app.tools.sai_email_interaction import SaiEmailGenericPlannerTool
from app.workers.sai_email_interaction_models import SaiEmailGenericPlan


class SaiEmailInteractionWorker:
    """Read `sai@...` threads, plan replies, and store evaluation traces."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.activity_store = SaiEmailActivityStore(settings.sai_email_activity_log_path)
        self.golden_store = SaiEmailGoldenDatasetStore(settings.sai_email_golden_dataset_path)

    def build_document_connector(self, *, policy: PolicyDocument) -> GmailDocumentConnector:
        return GmailDocumentConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy)
        )

    def build_send_connector(self, *, policy: PolicyDocument) -> GmailSendConnector:
        return GmailSendConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy),
            allowed_recipient_emails=self.allowed_reply_recipient_emails(policy=policy),
        )

    def required_actions(self, *, policy: PolicyDocument) -> list[ConnectorAction]:
        return [
            *self.build_document_connector(policy=policy).required_actions(),
            *self.build_send_connector(policy=policy).required_actions(),
        ]

    def connector_descriptors(self, *, policy: PolicyDocument) -> list[ConnectorDescriptor]:
        return [
            self.build_document_connector(policy=policy).describe(),
            self.build_send_connector(policy=policy).describe(),
        ]

    def plan_generic_request(
        self,
        *,
        request_message_id: str,
        thread_id: str,
        request_text: str,
        thread_state_summary: dict[str, object],
        task_context_summary: dict[str, object],
        known_facts: list[dict[str, object]],
        read_only_context: dict[str, object],
        workflow_catalog: list[dict[str, object]],
        prompt: PromptDocument,
        tool_definition: WorkflowToolDefinition,
    ) -> tuple[SaiEmailGenericPlan, ToolExecutionRecord]:
        planner = SaiEmailGenericPlannerTool(
            tool_definition=tool_definition,
            prompt=prompt,
            settings=self.settings,
        )
        return planner.plan(
            thread_id=thread_id,
            request_message_id=request_message_id,
            request_text=request_text,
            thread_state_summary=thread_state_summary,
            task_context_summary=task_context_summary,
            known_facts=known_facts,
            read_only_context=read_only_context,
            workflow_catalog=workflow_catalog,
        )

    def send_thread_reply(
        self,
        *,
        policy: PolicyDocument,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None,
    ) -> dict[str, Any]:
        connector = self.build_send_connector(policy=policy)
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        return connector.send_plaintext_message(
            to_email=to_email,
            subject=reply_subject,
            body=body,
            thread_id=thread_id,
            from_email=self.settings.sai_alias_email,
            reply_to_email=self.settings.sai_alias_email,
        )

    def allowed_request_sender_emails(self, *, policy: PolicyDocument) -> list[str]:
        raw_values = policy.gmail.get("allowed_request_sender_emails", [])
        if not isinstance(raw_values, list):
            return []
        return [
            value.strip().lower()
            for value in raw_values
            if isinstance(value, str) and value.strip()
        ]

    def allowed_reply_recipient_emails(self, *, policy: PolicyDocument) -> list[str]:
        raw_values = policy.gmail.get("allowed_reply_recipient_emails", [])
        if not isinstance(raw_values, list):
            return []
        return [
            value.strip().lower()
            for value in raw_values
            if isinstance(value, str) and value.strip()
        ]

    def is_allowed_request_sender(self, *, policy: PolicyDocument, from_email: str) -> bool:
        allowed = self.allowed_request_sender_emails(policy=policy)
        return not allowed or from_email.strip().lower() in set(allowed)

    def resolve_reply_recipient(self, *, policy: PolicyDocument, from_email: str) -> str:
        allowed = self.allowed_reply_recipient_emails(policy=policy)
        if allowed:
            return allowed[0]
        return from_email.strip().lower()

    def format_reply(self, *, short_response: str, explanation: str) -> str:
        compact_short = _truncate_compact(short_response, limit=160)
        compact_explanation = _truncate_words(explanation, max_words=100)
        return f"{compact_short}\n\n\n\n\n--- EXPLANATION:\n{compact_explanation}"

    def persist_activities(
        self,
        *,
        workflow_id: str,
        run_id: str,
        thread_id: str,
        message_id: str,
        activities: list[dict[str, Any]],
    ) -> None:
        rows = [
            SaiEmailActivityRecord(
                activity_id=str(activity["activity_id"]),
                thread_id=thread_id,
                message_id=message_id,
                workflow_id=workflow_id,
                run_id=run_id,
                recorded_at=utc_now(),
                activity_kind=str(activity.get("activity_kind", "unknown")),
                description=str(activity.get("description", "")).strip(),
                approval_required=bool(activity.get("approval_required", False)),
                metadata={
                    key: value
                    for key, value in activity.items()
                    if key
                    not in {
                        "activity_id",
                        "activity_kind",
                        "description",
                        "approval_required",
                    }
                },
            )
            for activity in activities
        ]
        self.activity_store.append_records(rows)

    def persist_golden_record(
        self,
        *,
        golden_id: str,
        thread_id: str,
        request_message_id: str,
        workflow_id: str,
        run_id: str,
        approved_by: str,
        request_kind: str,
        response_mode: str,
        short_response: str,
        explanation: str,
        activity_ids: list[str],
        approval_request_id: str | None,
        execution_status: str,
        metadata: dict[str, Any],
    ) -> None:
        self.golden_store.append_record(
            SaiEmailGoldenRecord(
                golden_id=golden_id,
                thread_id=thread_id,
                request_message_id=request_message_id,
                workflow_id=workflow_id,
                run_id=run_id,
                approved_at=utc_now(),
                approved_by=approved_by,
                request_kind=request_kind,
                response_mode=response_mode,
                short_response=_truncate_compact(short_response, limit=160),
                explanation=_truncate_words(explanation, max_words=100),
                activity_ids=list(activity_ids),
                approval_request_id=approval_request_id,
                execution_status=execution_status,  # type: ignore[arg-type]
                metadata=metadata,
            )
        )


def looks_like_email_approval(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return normalized in {"approve", "approved", "ok", "okay", "yes", "go ahead", "do it"}


def _truncate_compact(text: str, *, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _truncate_words(text: str, *, max_words: int) -> str:
    words = text.strip().split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip() + "…"
