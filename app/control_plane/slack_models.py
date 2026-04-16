"""Structured records for Slack questions, feedback, and status posts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SlackQuestionStatus = Literal[
    "pending",
    "answered",
    "approved",
    "skipped",
    "needs_context",
]
SlackFeedbackType = Literal["message", "action"]


class SlackQuestionRecord(BaseModel):
    """One Slack question posted by SAI about a workflow item."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    workflow_id: str
    run_id: str | None = None
    item_id: str
    channel_id: str
    thread_ts: str
    question_text: str
    status: SlackQuestionStatus = "pending"
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlackFeedbackRecord(BaseModel):
    """One inbound Slack feedback event tied to a question or generic thread."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str
    question_id: str | None = None
    workflow_id: str | None = None
    run_id: str | None = None
    item_id: str | None = None
    slack_user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    feedback_type: SlackFeedbackType
    text: str | None = None
    action_id: str | None = None
    value: str | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlackInteractionDecision(BaseModel):
    """Structured button decision parsed from one Slack interaction payload."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    decision: str
    slack_user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    workflow_id: str | None = None
    run_id: str | None = None
    item_id: str | None = None
    approval_request_id: str | None = None


class SlackTaskRequest(BaseModel):
    """Structured direct task request parsed from an inbound Slack DM."""

    model_config = ConfigDict(extra="forbid")

    slack_user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    task_text: str
    feedback_id: str
