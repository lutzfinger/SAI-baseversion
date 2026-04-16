"""Common models for tool execution traces."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolExecutionStatus(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FALLBACK = "fallback"
    FAILED = "failed"


class ToolExecutionRecord(BaseModel):
    """Structured trace for one tool invocation inside a workflow."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    tool_kind: str
    status: ToolExecutionStatus
    details: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionError(RuntimeError):
    """Exception wrapper for tool failures that should still be logged structurally."""

    def __init__(
        self,
        message: str,
        *,
        tool_record: ToolExecutionRecord,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_record = tool_record
        self.context = context or {}
