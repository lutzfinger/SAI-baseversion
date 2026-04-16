"""Minimal reflection schemas used by the run store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReflectionFinding(BaseModel):
    """One suggestion-only reflection finding tied to prior runs."""

    title: str
    summary: str
    severity: str = "info"
    recommendation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionReport(BaseModel):
    """Stored reflection output that never auto-applies changes."""

    report_id: str
    workflow_id: str
    generated_at: datetime
    summary: str
    findings: list[ReflectionFinding] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
