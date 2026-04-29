"""Template-based internal and external drafting for meeting workflows."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.meeting_models import (
    MeetingDraftPackage,
    MeetingEvidence,
    MeetingLikelihoodAssessment,
)


class InternalBriefWriterTool:
    """Create an operator-facing explanation for one meeting decision."""

    def __init__(self, *, tool_id: str, template_config: dict[str, Any]) -> None:
        self.tool_id = tool_id
        self.template_config = template_config

    def write(
        self,
        *,
        message: EmailMessage,
        evidence: MeetingEvidence,
        assessment: MeetingLikelihoodAssessment,
    ) -> tuple[str, ToolExecutionRecord]:
        template = str(
            self.template_config.get(
                "template",
                "Decision: {decision}\nScore: {score}\nWhy:\n{reasons}",
            )
        )
        reasons = (
            "\n".join(f"- {reason}" for reason in assessment.reasons)
            or "- No reasons recorded"
        )
        note = template.format(
            contact_name=message.from_name or message.from_email,
            contact_email=message.from_email,
            subject=message.subject,
            decision=assessment.decision,
            score=f"{assessment.likelihood_score:.2f}",
            request_type=evidence.request_type,
            reasons=reasons,
            prior_contact=evidence.gmail_history.get("prior_total_count", 0),
            prior_meetings=evidence.meetings_in_last_12_months
            or evidence.calendar_history.get("prior_meeting_count", 0),
            linkedin_match="yes" if evidence.linkedin.get("matched") else "no",
        ).strip()
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="internal_brief_writer",
            status=ToolExecutionStatus.COMPLETED,
            details={"chars": len(note)},
        )
        return note, record


class ReplyDraftWriterTool:
    """Create the external draft body based on the likelihood decision."""

    def __init__(
        self,
        *,
        tool_id: str,
        template_config: dict[str, Any],
        calendar_link: str,
    ) -> None:
        self.tool_id = tool_id
        self.template_config = template_config
        self.calendar_link = calendar_link

    def write(
        self,
        *,
        message: EmailMessage,
        assessment: MeetingLikelihoodAssessment,
        internal_note: str,
    ) -> tuple[MeetingDraftPackage, ToolExecutionRecord]:
        contact_name = message.from_name or "there"
        subject = self._reply_subject(message.subject)
        reasons_text = "; ".join(assessment.reasons) or "I do not have enough context yet."
        if assessment.decision == "send_calendar_link":
            body = str(
                self.template_config.get(
                    "send_calendar_link_template",
                    "Let's meet. Please see my calendar here: {calendar_link} - "
                    "Is there a suitable time for you?",
                )
            ).format(
                contact_name=contact_name,
                calendar_link=self.calendar_link,
                reasons=reasons_text,
            )
        elif assessment.decision == "ask_for_more_info":
            body = str(
                self.template_config.get(
                    "ask_for_more_info_template",
                    "Thanks for reaching out. Before we schedule, could you share "
                    "a bit more detail on the topic, the goal for the "
                    "conversation, and who would join?",
                )
            ).format(
                contact_name=contact_name,
                calendar_link=self.calendar_link,
                reasons=reasons_text,
            )
        else:
            body = str(
                self.template_config.get(
                    "manual_review_template",
                    "Manual review required before sending a reply.",
                )
            ).format(
                contact_name=contact_name,
                calendar_link=self.calendar_link,
                reasons=reasons_text,
            )

        draft = MeetingDraftPackage(
            message_id=message.message_id,
            internal_note=internal_note,
            external_subject=subject,
            external_body=body.strip(),
            draft_created=False,
            draft_id=None,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="reply_draft_writer",
            status=ToolExecutionStatus.COMPLETED,
            details={"decision": assessment.decision, "chars": len(draft.external_body)},
        )
        return draft, record

    def _reply_subject(self, subject: str) -> str:
        lowered = subject.lower()
        return subject if lowered.startswith("re:") else f"Re: {subject}"
