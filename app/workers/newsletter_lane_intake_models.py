"""Models for operator-approved newsletter lane rules."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.learning.newsletter_lane_registry import NewsletterLaneMatchScope, NewsletterLaneRoute
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailMessage


class NewsletterLaneIntakeResult(BaseModel):
    """Outcome of recording one newsletter lane rule."""

    model_config = ConfigDict(extra="forbid")

    message_reference: str
    matched_message: EmailMessage
    route: NewsletterLaneRoute
    match_scope: NewsletterLaneMatchScope
    match_value: str
    recorded: bool
    duplicates_skipped: int = 0
    tool_records: list[ToolExecutionRecord]
