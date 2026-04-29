"""Strict tool for looking up past calendar meetings for one email sender."""

from __future__ import annotations

from app.connectors.calendar import CalendarHistoryConnector
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.meeting_models import CalendarMeetingHistoryResult


class CalendarMeetingHistoryLookupTool:
    """Return a strict JSON-style summary of past meetings for one email sender."""

    def __init__(
        self,
        *,
        tool_id: str,
        calendar_history: CalendarHistoryConnector,
        lookback_days: int = 365,
    ) -> None:
        self.tool_id = tool_id
        self.calendar_history = calendar_history
        self.lookback_days = lookback_days

    def lookup(
        self,
        *,
        message: EmailMessage,
    ) -> tuple[CalendarMeetingHistoryResult, ToolExecutionRecord]:
        summary = self.calendar_history.summarize_contact(
            contact_email=message.from_email,
            lookback_days=self.lookback_days,
            contact_name=message.from_name,
        )
        result = CalendarMeetingHistoryResult.model_validate(
            {
                "message_id": message.message_id,
                "contact_email": message.from_email,
                "contact_name": message.from_name,
                "lookback_days": summary.get("lookback_days", self.lookback_days),
                "meetings_in_last_12_months": summary.get("prior_meeting_count", 0),
                "upcoming_meeting_count": summary.get("upcoming_meeting_count", 0),
                "has_met_in_last_12_months": summary.get("has_prior_meeting", False),
                "last_meeting_at": summary.get("last_meeting_at"),
            }
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="calendar_meeting_history_lookup",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "message_id": result.message_id,
                "contact_email": result.contact_email,
                "lookback_days": result.lookback_days,
                "meetings_in_last_12_months": result.meetings_in_last_12_months,
                "upcoming_meeting_count": result.upcoming_meeting_count,
                "has_met_in_last_12_months": result.has_met_in_last_12_months,
            },
        )
        return result, record
