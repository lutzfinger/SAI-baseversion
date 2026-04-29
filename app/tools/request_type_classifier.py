"""Deterministic request-type classifier for meeting workflows."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.meeting_models import MeetingRequestType


class RequestTypeClassifierTool:
    """Classify the kind of meeting request from subject and snippet text."""

    def __init__(self, *, tool_id: str, classifier_config: dict[str, Any]) -> None:
        self.tool_id = tool_id
        self.classifier_config = classifier_config

    def classify(self, *, message: EmailMessage) -> tuple[MeetingRequestType, ToolExecutionRecord]:
        full_text = message.combined_text().lower()
        matched_keywords: list[str] = []

        request_type_keywords = self.classifier_config.get("request_type_keywords", {})
        for request_type, keywords in request_type_keywords.items():
            lowered_keywords = [str(keyword).lower() for keyword in keywords]
            matching = [keyword for keyword in lowered_keywords if keyword in full_text]
            if matching:
                matched_keywords = matching
                record = ToolExecutionRecord(
                    tool_id=self.tool_id,
                    tool_kind="request_type_classifier",
                    status=ToolExecutionStatus.COMPLETED,
                    details={"request_type": request_type, "matched_keywords": matching},
                )
                return request_type, record

        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="request_type_classifier",
            status=ToolExecutionStatus.COMPLETED,
            details={"request_type": "unknown", "matched_keywords": matched_keywords},
        )
        return "unknown", record
