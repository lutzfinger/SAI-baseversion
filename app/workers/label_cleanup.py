"""Worker for clearing incomplete Gmail taxonomy labels."""

from __future__ import annotations

from typing import cast

from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_labels import GmailLabelConnector
from app.shared.config import Settings
from app.shared.models import PolicyDocument
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.label_cleanup_models import LabelCleanupItem


class LabelCleanupWorker:
    """Remove taxonomy labels from threads that only have L1 or only L2."""

    def __init__(
        self,
        *,
        settings: Settings,
        gmail_labels: GmailLabelConnector | None = None,
    ) -> None:
        self.settings = settings
        self._gmail_labels = gmail_labels

    def build_label_connector(self, *, policy: PolicyDocument) -> GmailLabelConnector:
        return self._gmail_labels or GmailLabelConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy),
        )

    def cleanup_labels(
        self,
        *,
        messages: list[EmailMessage],
        policy: PolicyDocument,
        gmail_labels: GmailLabelConnector | None = None,
    ) -> list[LabelCleanupItem]:
        connector = gmail_labels or self.build_label_connector(policy=policy)
        items: list[LabelCleanupItem] = []
        for message in messages:
            thread_id = message.thread_id or message.message_id
            current_label_names = connector.list_thread_taxonomy_labels(thread_id=thread_id)
            has_l1 = any(name.startswith(("L1/", "SAI/L1/")) for name in current_label_names)
            has_l2 = any(name.startswith(("L2/", "SAI/L2/")) for name in current_label_names)
            if has_l1 and has_l2:
                items.append(
                    LabelCleanupItem(
                        message=message,
                        current_label_names=current_label_names,
                        status="kept_complete_labels",
                        tool_records=[
                            ToolExecutionRecord(
                                tool_id="gmail_label_cleanup",
                                tool_kind="gmail_label_cleanup",
                                status=ToolExecutionStatus.SKIPPED,
                                details={
                                    "thread_id": thread_id,
                                    "current_label_names": current_label_names,
                                    "reason": "Thread still has both L1 and L2 labels.",
                                },
                            )
                        ],
                    )
                )
                continue

            cleanup_payload = connector.clear_thread_taxonomy_labels(thread_id=thread_id)
            removed_label_names = cast(
                list[object],
                cleanup_payload["removed_label_names"],
            )
            removed_label_ids = cast(
                list[object],
                cleanup_payload["removed_label_ids"],
            )
            items.append(
                LabelCleanupItem(
                    message=message,
                    current_label_names=current_label_names,
                    removed_label_names=[str(value) for value in removed_label_names],
                    removed_label_ids=[str(value) for value in removed_label_ids],
                    status="cleared_partial_labels",
                    tool_records=[
                        ToolExecutionRecord(
                            tool_id="gmail_label_cleanup",
                            tool_kind="gmail_label_cleanup",
                            status=ToolExecutionStatus.COMPLETED,
                            details=cleanup_payload,
                        )
                    ],
                )
            )
        return items
