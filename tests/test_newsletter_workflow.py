from __future__ import annotations

import json

from pytest import MonkeyPatch

from app.connectors.gmail import GmailAPIConnector
from app.connectors.gmail_labels import GmailLabelConnector
from app.control_plane.runner import ControlPlane
from app.shared.config import Settings
from app.tools.local_llm_classifier import MockJSONClient, StructuredEmailClassifierTool
from app.workers.email_models import EmailMessage, EmailThreadTagResult


def test_newsletter_tagging_workflow_writes_eval_and_artifact(
    monkeypatch: MonkeyPatch,
    starter_settings: Settings,
) -> None:
    message = EmailMessage(
        message_id="msg-1",
        thread_id="thread-1",
        from_email="author@example.org",
        to=["you@example.com"],
        subject="Weekly newsletter",
        snippet="unsubscribe any time",
        body_excerpt="A weekly digest for subscribers.",
    )

    monkeypatch.setattr(
        GmailAPIConnector,
        "fetch_messages",
        lambda self: [message],
    )
    monkeypatch.setattr(
        StructuredEmailClassifierTool,
        "_build_client",
        lambda self: MockJSONClient(),
    )
    monkeypatch.setattr(
        GmailLabelConnector,
        "apply_thread_tags",
        lambda self, request: EmailThreadTagResult(
            thread_id=request.thread_id,
            applied_label_names=request.gmail_label_names(),
            archived_from_inbox=request.archive_from_inbox,
        ),
    )

    control_plane = ControlPlane(starter_settings)
    result = control_plane.run_workflow(workflow_id="newsletter-identification-gmail-tagging")

    assert result.status == "completed"
    assert result.summary["classified_message_count"] == 1
    assert result.summary["newsletter_message_count"] == 1
    assert result.summary["tagged_thread_count"] == 1

    eval_rows = (
        starter_settings.newsletter_eval_dataset_path.read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(eval_rows) == 1
    eval_row = json.loads(eval_rows[0])
    assert eval_row["message_id"] == "msg-1"
    assert eval_row["predicted_level1"] == "newsletter"

    workflow_items = control_plane.run_store.list_workflow_items(
        workflow_id="newsletter-identification-gmail-tagging"
    )
    assert len(workflow_items) == 1
    assert workflow_items[0]["item_id"] == "thread-1"
    assert workflow_items[0]["metadata"]["last_processed_message_id"] == "msg-1"
