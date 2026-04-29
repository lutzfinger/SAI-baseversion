"""Typed models for SAI's shared task plane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskChannel = Literal["email", "slack", "codex", "workflow"]
TaskStatus = Literal[
    "awaiting_information",
    "awaiting_approval",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
]
TaskStepStatus = Literal["pending", "blocked", "completed", "failed"]


class TaskRecord(BaseModel):
    """One operator-facing task tracked independently of any single workflow run."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    channel: TaskChannel
    workflow_id: str
    task_type: str
    status: TaskStatus
    title: str
    summary: str
    current_plan: dict[str, Any] = Field(default_factory=dict)
    pending_question: str | None = None
    approval_request_id: str | None = None
    source_thread_id: str | None = None
    source_message_id: str | None = None
    latest_response_message_id: str | None = None
    latest_run_id: str | None = None
    opaque_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TaskStepRecord(BaseModel):
    """One current step snapshot for a tracked task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    step_id: str
    sequence: int
    step_kind: str
    description: str
    status: TaskStepStatus
    approval_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)
