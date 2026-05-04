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


# ─── Slack Web API response models (per #6a — every network response
# validates against an explicit shape) ────────────────────────────────


class _SlackResponseBase(BaseModel):
    """Common base for every Slack Web API response.

    Slack adds new fields to responses without notice. We use
    ``extra="ignore"`` so additions don't break validation, but every
    field we actually READ must be declared and typed — drift in a
    field we use surfaces as a ``ValidationError`` rather than a
    silent ``None`` downstream.
    """

    model_config = ConfigDict(extra="ignore")

    ok: bool
    error: str | None = None


class SlackChatPostMessageResponse(_SlackResponseBase):
    """Response shape from ``chat.postMessage``."""

    channel: str | None = None
    ts: str | None = None


class SlackConversationsHistoryResponse(_SlackResponseBase):
    """Response shape from ``conversations.history``.

    ``messages`` is a list of opaque Slack message dicts; we don't
    re-validate each message structure because consumers may use
    different fields and Slack's per-message shape is huge.
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)


class _SlackChannelStub(BaseModel):
    """Minimal channel shape we read from Slack — just enough for
    name→id resolution + DM channel resolution."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None


class SlackConversationsListResponse(_SlackResponseBase):
    """Response shape from ``conversations.list``."""

    channels: list[_SlackChannelStub] = Field(default_factory=list)
    response_metadata: dict[str, Any] = Field(default_factory=dict)


class SlackConversationsOpenResponse(_SlackResponseBase):
    """Response shape from ``conversations.open`` (DM creation)."""

    channel: _SlackChannelStub | None = None


class _SlackFileStub(BaseModel):
    """Minimal file shape we read from ``files_upload_v2``."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    permalink: str | None = None


class SlackFilesUploadResponse(_SlackResponseBase):
    """Response shape from ``files_upload_v2``."""

    file: _SlackFileStub | None = None
