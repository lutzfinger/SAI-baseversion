"""Template-based draft creation for contact investigation findings."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.contact_investigation_models import (
    ContactInvestigationAssessment,
    ContactInvestigationDraftPackage,
)
from app.workers.email_models import EmailClassification, EmailMessage


class ContactInvestigationDraftWriterTool:
    """Write an internal draft to Lutz summarizing deeper relationship findings."""

    def __init__(
        self,
        *,
        tool_id: str,
        template_config: dict[str, Any],
        operator_email: str,
    ) -> None:
        self.tool_id = tool_id
        self.template_config = template_config
        self.operator_email = operator_email

    def write(
        self,
        *,
        message: EmailMessage,
        email_classification: EmailClassification,
        gmail_history: dict[str, Any],
        calendar_history: dict[str, Any],
        linkedin: dict[str, Any],
        assessment: ContactInvestigationAssessment,
        draft_recipient: str | None = None,
    ) -> tuple[ContactInvestigationDraftPackage, ToolExecutionRecord]:
        template = str(
            self.template_config.get(
                "template",
                "Investigation for {contact_name} ({contact_email})\n"
                "Original Level 1: {original_level1}\n"
                "Suggested Level 1: {suggested_level1}\n"
                "Known relationship: {known_relationship}\n"
                "Prior Gmail contacts: {prior_contacts}\n"
                "Met in last 12 months: {met_before}\n"
                "Prior meetings: {prior_meetings}\n"
                "Last meeting at: {last_meeting_at}\n"
                "LinkedIn match: {linkedin_match}\n"
                "LinkedIn degree: {linkedin_degree}\n"
                "LinkedIn profile: {linkedin_profile}\n"
                "LinkedIn notes: {linkedin_notes}\n"
                "\nWhy:\n{reasons}",
            )
        )
        reasons = "\n".join(f"- {reason}" for reason in assessment.reasons)
        draft_subject = (
            message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
        )
        draft_body = template.format(
            contact_name=message.from_name or message.from_email,
            contact_email=message.from_email,
            subject=message.subject,
            original_level1=email_classification.level1_classification,
            suggested_level1=assessment.suggested_level1_classification,
            known_relationship=assessment.known_relationship,
            prior_contacts=gmail_history.get("prior_total_count", 0),
            met_before="yes"
            if (
                bool(calendar_history.get("met_before_in_last_12_months"))
                or bool(calendar_history.get("has_prior_meeting"))
                or int(
                    calendar_history.get(
                        "meetings_in_last_12_months",
                        calendar_history.get("prior_meeting_count", 0),
                    )
                )
                > 0
            )
            else "no",
            prior_meetings=calendar_history.get(
                "meetings_in_last_12_months",
                calendar_history.get("prior_meeting_count", 0),
            ),
            last_meeting_at=calendar_history.get("last_meeting_at") or "none recorded",
            linkedin_match="yes" if linkedin.get("matched") else "no",
            linkedin_degree=linkedin.get("connection_degree") or "unknown",
            linkedin_profile=linkedin.get("profile_url") or "not available",
            linkedin_notes=linkedin.get("notes") or "none",
            reasons=reasons or "- No stronger relationship evidence found.",
        ).strip()
        draft = ContactInvestigationDraftPackage(
            message_id=message.message_id,
            to_email=draft_recipient or self.operator_email,
            draft_subject=draft_subject,
            draft_body=draft_body,
            thread_id=message.thread_id,
            draft_created=False,
            draft_id=None,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="contact_investigation_draft_writer",
            status=ToolExecutionStatus.COMPLETED,
            details={"chars": len(draft_body)},
        )
        return draft, record
