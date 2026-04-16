from __future__ import annotations

import json
from datetime import UTC, datetime

from app.connectors.gmail import GmailAPIConnector
from app.connectors.gmail_documents import GmailDocumentConnector
from app.connectors.gmail_send import GmailSendConnector
from app.control_plane.runner import ControlPlane
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailDocument, EmailMessage
from app.workers.sai_email_interaction_models import (
    SaiEmailActivity,
    SaiEmailGenericPlan,
)
from app.workers.task_assistant_models import TaskAction, TaskExecutionOutcome, TaskExecutionPlan


def test_starter_email_interaction_creates_task_and_reply(
    monkeypatch,
    starter_settings,
) -> None:
    request_message = EmailMessage(
        message_id="email-1",
        thread_id="thread-1",
        from_email="you@example.com",
        to=["sai@example.com"],
        subject="Please tag newsletters",
        snippet="Can you run newsletter tagging?",
        body_excerpt="Please review the inbox and tag newsletters.",
    )

    monkeypatch.setattr(
        GmailAPIConnector,
        "fetch_messages",
        lambda self: [request_message],
    )
    monkeypatch.setattr(
        GmailAPIConnector,
        "fetch_thread_messages",
        lambda self, thread_id: [request_message],
    )
    monkeypatch.setattr(
        GmailDocumentConnector,
        "fetch_document",
        lambda self, message_id: EmailDocument(
            message=request_message,
            plain_text="Please run newsletter tagging on this inbox slice.",
        ),
    )

    sent_messages: list[dict[str, str]] = []

    def fake_send(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        from_email: str | None = None,
        reply_to_email: str | None = None,
    ) -> dict[str, str]:
        sent_messages.append(
            {
                "to_email": to_email,
                "subject": subject,
                "body": body,
                "thread_id": thread_id or "",
                "from_email": from_email or "",
                "reply_to_email": reply_to_email or "",
            }
        )
        return {
            "message_id": "reply-1",
            "thread_id": thread_id or "",
            "to_email": to_email,
            "subject": subject,
            "from_email": from_email or "",
            "reply_to_email": reply_to_email or "",
        }

    monkeypatch.setattr(GmailSendConnector, "send_plaintext_message", fake_send)

    def fake_plan(*args, **kwargs):
        del args, kwargs
        return (
            SaiEmailGenericPlan(
                response_mode="ask_approval",
                short_response="I can run newsletter tagging for this inbox slice. Approve?",
                explanation="This maps to the starter newsletter workflow and needs approval.",
                activities=[
                    SaiEmailActivity(
                        activity_id="1",
                        activity_kind="plan",
                        description="Mapped the request to the newsletter workflow.",
                        approval_required=True,
                    )
                ],
                request_kind="workflow_suggestion",
                execution_plan=TaskExecutionPlan(
                    task_summary="Run newsletter tagging",
                    approach_summary="Execute the newsletter tagging workflow after approval.",
                    operator_approval_question="Approve newsletter tagging?",
                    risk_level="moderate",
                    actions=[
                        TaskAction(
                            action_id="run-1",
                            action_kind="run_workflow",
                            purpose="Run the starter newsletter tagging workflow.",
                            workflow_id="newsletter-identification-gmail-tagging",
                        )
                    ],
                    confidence=0.82,
                    rationale="The request directly matches the starter workflow catalog.",
                    safety_notes=["This modifies Gmail labels."],
                ),
            ),
            ToolExecutionRecord(
                tool_id="sai_email_planner",
                tool_kind="sai_email_planner",
                status=ToolExecutionStatus.COMPLETED,
                details={"provider": "mock"},
            ),
        )

    control_plane = ControlPlane(starter_settings)
    monkeypatch.setattr(
        control_plane.sai_email_worker,
        "plan_generic_request",
        fake_plan,
    )

    result = control_plane.run_workflow(workflow_id="starter-email-interaction")

    assert result.status == "completed"
    assert result.summary["replied_count"] == 1
    assert result.summary["awaiting_approval_count"] == 1
    assert sent_messages[0]["to_email"] == "you@example.com"
    assert "--- EXPLANATION:" in sent_messages[0]["body"]

    tasks = control_plane.run_store.list_tasks(
        workflow_id="starter-email-interaction",
    )
    assert len(tasks) == 1
    assert tasks[0].status == "awaiting_approval"
    assert tasks[0].task_kind == "workflow_suggestion"

    activity_rows = (
        starter_settings.sai_email_activity_log_path.read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(activity_rows) == 1
    activity_row = json.loads(activity_rows[0])
    assert activity_row["thread_id"] == "thread-1"
    assert activity_row["activity_kind"] == "plan"


def test_starter_email_interaction_approval_executes_and_writes_golden(
    monkeypatch,
    starter_settings,
) -> None:
    request_message = EmailMessage(
        message_id="email-1",
        thread_id="thread-1",
        from_email="you@example.com",
        to=["sai@example.com"],
        subject="Please tag newsletters",
        snippet="Can you run newsletter tagging?",
        body_excerpt="Please review the inbox and tag newsletters.",
    )
    approval_message = EmailMessage(
        message_id="email-2",
        thread_id="thread-1",
        from_email="you@example.com",
        to=["you@example.com"],
        subject="Re: Please tag newsletters",
        snippet="approved",
        body_excerpt="approved",
    )
    approval_document_message = approval_message.model_copy(
        update={"subject": "", "snippet": "", "body_excerpt": ""}
    )

    sent_messages: list[dict[str, str]] = []

    def fake_send(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        from_email: str | None = None,
        reply_to_email: str | None = None,
    ) -> dict[str, str]:
        sent_messages.append({"to_email": to_email, "subject": subject, "body": body})
        return {
            "message_id": f"reply-{len(sent_messages)}",
            "thread_id": thread_id or "",
            "to_email": to_email,
            "subject": subject,
            "from_email": from_email or "",
            "reply_to_email": reply_to_email or "",
        }

    control_plane = ControlPlane(starter_settings)
    stage = {"value": "request"}

    def fake_fetch_messages(self):
        return [request_message] if stage["value"] == "request" else []

    def fake_fetch_thread_messages(self, thread_id: str):
        del thread_id
        return (
            [request_message]
            if stage["value"] == "request"
            else [request_message, approval_message]
        )

    def fake_fetch_document(self, *, message_id: str):
        if message_id == "email-1":
            return EmailDocument(
                message=request_message,
                plain_text="Please run newsletter tagging on this inbox slice.",
            )
        return EmailDocument(message=approval_document_message, plain_text="approved")

    monkeypatch.setattr(GmailAPIConnector, "fetch_messages", fake_fetch_messages)
    monkeypatch.setattr(
        GmailAPIConnector,
        "fetch_thread_messages",
        fake_fetch_thread_messages,
    )
    monkeypatch.setattr(GmailDocumentConnector, "fetch_document", fake_fetch_document)
    monkeypatch.setattr(GmailSendConnector, "send_plaintext_message", fake_send)

    def fake_plan(*args, **kwargs):
        del args, kwargs
        return (
            SaiEmailGenericPlan(
                response_mode="ask_approval",
                short_response="I can run newsletter tagging for this inbox slice. Approve?",
                explanation="This maps to the starter newsletter workflow and needs approval.",
                activities=[
                    SaiEmailActivity(
                        activity_id="1",
                        activity_kind="plan",
                        description="Mapped the request to the newsletter workflow.",
                        approval_required=True,
                    )
                ],
                request_kind="workflow_suggestion",
                execution_plan=TaskExecutionPlan(
                    task_summary="Run newsletter tagging",
                    approach_summary="Execute the newsletter tagging workflow after approval.",
                    operator_approval_question="Approve newsletter tagging?",
                    risk_level="moderate",
                    actions=[
                        TaskAction(
                            action_id="run-1",
                            action_kind="run_workflow",
                            purpose="Run the starter newsletter tagging workflow.",
                            workflow_id="newsletter-identification-gmail-tagging",
                        )
                    ],
                    confidence=0.82,
                    rationale="The request directly matches the starter workflow catalog.",
                    safety_notes=["This modifies Gmail labels."],
                ),
            ),
            ToolExecutionRecord(
                tool_id="sai_email_planner",
                tool_kind="sai_email_planner",
                status=ToolExecutionStatus.COMPLETED,
                details={"provider": "mock"},
            ),
        )

    monkeypatch.setattr(
        control_plane.sai_email_worker,
        "plan_generic_request",
        fake_plan,
    )
    monkeypatch.setattr(
        control_plane,
        "_execute_task_plan",
        lambda **kwargs: TaskExecutionOutcome(
            approved=True,
            approved_by=kwargs["approved_by"],
            approved_at=datetime.now(UTC),
            step_results=[],
            completed_action_count=1,
            failed_action_count=0,
        ),
    )

    first_result = control_plane.run_workflow(workflow_id="starter-email-interaction")
    assert first_result.summary["awaiting_approval_count"] == 1

    stage["value"] = "approval"
    second_result = control_plane.run_workflow(workflow_id="starter-email-interaction")

    assert second_result.status == "completed"
    assert second_result.summary["completed_count"] == 1
    assert sent_messages[-1]["body"].startswith("Done.")

    tasks = control_plane.run_store.list_tasks(
        workflow_id="starter-email-interaction",
    )
    assert len(tasks) == 1
    assert tasks[0].status == "completed"

    golden_rows = (
        starter_settings.sai_email_golden_dataset_path.read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(golden_rows) == 1
    golden_row = json.loads(golden_rows[0])
    assert golden_row["request_message_id"] == "email-1"
    assert golden_row["execution_status"] == "completed"
