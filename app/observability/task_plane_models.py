"""Typed models for SAI's unified task plane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.registry import validate_task_kind_name

TaskStatus = Literal[
    "in_progress",
    "awaiting_information",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]

TaskStepStatus = Literal[
    "pending",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]

TaskEventKind = Literal[
    "task_created",
    "task_updated",
    "reply_prepared",
    "reply_sending",
    "reply_sent",
    "reply_delivery_uncertain",
    "reply_send_failed",
    "approval_requested",
    "approval_resolved",
    "step_recorded",
]


class TaskRecord(BaseModel):
    """Current recoverable state for one operator task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    workflow_id: str
    source_kind: str
    source_thread_id: str | None = None
    source_message_id: str | None = None
    requested_by: str | None = None
    title: str
    task_kind: str | None = None
    status: TaskStatus
    current_plan: dict[str, Any] = Field(default_factory=dict)
    pending_question: str | None = None
    approval_request_ids: list[str] = Field(default_factory=list)
    linked_thread_ids: list[str] = Field(default_factory=list)
    linked_message_ids: list[str] = Field(default_factory=list)
    opaque_payload: dict[str, Any] = Field(default_factory=dict)
    last_run_id: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator("task_kind")
    @classmethod
    def validate_task_kind(cls, value: str | None) -> str | None:
        return validate_task_kind_name(value)


class TaskStepRecord(BaseModel):
    """Materialized current state for one step within a recoverable task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    step_id: str
    workflow_id: str
    run_id: str
    step_kind: str
    description: str
    status: TaskStepStatus
    approval_required: bool = False
    sequence_number: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class TaskEventRecord(BaseModel):
    """Append-only task-plane event for recovery and audit."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    task_id: str
    workflow_id: str
    run_id: str
    event_kind: TaskEventKind
    summary: str
    step_id: str | None = None
    step_kind: str | None = None
    status: str | None = None
    sequence_number: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)
