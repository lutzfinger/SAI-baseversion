"""Deterministic starter email pre-filter for obvious newsletter routing."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailClassification, EmailMessage


class KeywordEmailClassifierTool:
    """Classify only clear, filter-like newsletter signals."""

    def __init__(self, *, tool_id: str, classifier_config: dict[str, Any]) -> None:
        self.tool_id = tool_id
        self.classifier_config = classifier_config

    def classify(
        self,
        *,
        message: EmailMessage,
        operator_email: str,
    ) -> tuple[EmailClassification, ToolExecutionRecord]:
        del operator_email
        sender = message.from_email.strip().lower()
        sender_domain = sender.split("@", 1)[1] if "@" in sender else sender
        newsletter_sender_emails = {
            str(value).strip().lower()
            for value in self.classifier_config.get("newsletter_sender_emails", [])
            if str(value).strip()
        }
        newsletter_sender_domains = {
            str(value).strip().lower()
            for value in self.classifier_config.get("newsletter_sender_domains", [])
            if str(value).strip()
        }
        newsletter_subject_keywords = {
            str(value).strip().lower()
            for value in self.classifier_config.get("newsletter_subject_keywords", [])
            if str(value).strip()
        }
        combined_text = message.combined_text().lower()
        matched_reason = "no deterministic newsletter signal matched"
        match_type = "fallback"
        classification = EmailClassification(
            message_id=message.message_id,
            level1_classification="other",
            level2_intent="others",
            confidence=0.24,
            reason="No deterministic newsletter rule matched.",
        )

        if sender in newsletter_sender_emails:
            classification = EmailClassification(
                message_id=message.message_id,
                level1_classification="newsletter",
                level2_intent="informational",
                confidence=0.97,
                reason="Exact sender email matched the newsletter rule set.",
            )
            matched_reason = classification.reason
            match_type = "sender_email"
        elif sender_domain in newsletter_sender_domains or any(
            sender_domain.endswith(f".{domain}") for domain in newsletter_sender_domains
        ):
            classification = EmailClassification(
                message_id=message.message_id,
                level1_classification="newsletter",
                level2_intent="informational",
                confidence=0.9,
                reason="Sender domain matched the newsletter rule set.",
            )
            matched_reason = classification.reason
            match_type = "sender_domain"
        elif message.list_unsubscribe or message.unsubscribe_links:
            classification = EmailClassification(
                message_id=message.message_id,
                level1_classification="newsletter",
                level2_intent="informational",
                confidence=0.88,
                reason="List-unsubscribe headers or links matched a newsletter rule.",
            )
            matched_reason = classification.reason
            match_type = "unsubscribe_signal"
        elif any(keyword in combined_text for keyword in newsletter_subject_keywords):
            classification = EmailClassification(
                message_id=message.message_id,
                level1_classification="newsletter",
                level2_intent="informational",
                confidence=0.82,
                reason="A configured newsletter keyword matched the subject/body excerpt.",
            )
            matched_reason = classification.reason
            match_type = "keyword"

        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="keyword_classifier",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "match_type": match_type,
                "resolved_level1": classification.level1_classification != "other",
                "matched_reason": matched_reason,
            },
        )
        return classification, record
