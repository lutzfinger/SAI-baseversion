"""Starter Gmail tagging tool."""

from __future__ import annotations

from app.connectors.gmail_labels import GmailLabelConnector
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import (
    EmailClassification,
    EmailThreadTagRequest,
    EmailThreadTagResult,
)


class GmailThreadTaggerTool:
    """Apply starter taxonomy labels to a thread through the Gmail label connector."""

    def __init__(self, *, tool_id: str, connector: GmailLabelConnector) -> None:
        self.tool_id = tool_id
        self.connector = connector

    def tag_thread(
        self,
        *,
        thread_id: str,
        classification: EmailClassification,
        archive_from_inbox: bool,
    ) -> tuple[EmailThreadTagResult | None, ToolExecutionRecord]:
        if not thread_id:
            return None, ToolExecutionRecord(
                tool_id=self.tool_id,
                tool_kind="gmail_thread_tagger",
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "missing_thread_id"},
            )
        label_names = classification.gmail_label_names()
        if not label_names:
            return None, ToolExecutionRecord(
                tool_id=self.tool_id,
                tool_kind="gmail_thread_tagger",
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "no_labels_to_apply"},
            )
        result = self.connector.apply_thread_tags(
            EmailThreadTagRequest(
                thread_id=thread_id,
                classification=classification,
                archive_from_inbox=archive_from_inbox,
            )
        )
        return result, ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="gmail_thread_tagger",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "thread_id": thread_id,
                "applied_label_names": result.applied_label_names,
                "archived_from_inbox": result.archived_from_inbox,
            },
        )
