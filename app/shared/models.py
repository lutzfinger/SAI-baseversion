"""Shared schemas used across the control plane.

These models are the typed contract between the layers described in the
original architecture plan: control plane, workers, approvals, reflection, and
observability all speak in terms of these objects.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RunStatus(StrEnum):
    """Lifecycle states for a workflow run in the orchestrator."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


class ApprovalStatus(StrEnum):
    """States for a human decision in the approval subsystem."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class PolicyMode(StrEnum):
    """Policy outcomes used by the approval layer."""

    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class PolicyRule(BaseModel):
    """One rule from a policy file, matched by action name or wildcard."""

    action: str
    mode: PolicyMode
    reason: str | None = None


class PromptDocument(BaseModel):
    """Prompt file plus parsed metadata and traceability hash."""

    prompt_id: str
    version: str
    description: str | None = None
    instructions: str
    config: dict[str, Any] = Field(default_factory=dict)
    sha256: str
    path: Path


class PolicyDocument(BaseModel):
    """Policy file plus evaluation helpers used at runtime."""

    policy_id: str
    version: str
    description: str | None = None
    default_mode: PolicyMode = PolicyMode.DENY
    rules: list[PolicyRule] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=dict)
    gmail: dict[str, Any] = Field(default_factory=dict)
    calendar: dict[str, Any] = Field(default_factory=dict)
    slack: dict[str, Any] = Field(default_factory=dict)
    web: dict[str, Any] = Field(default_factory=dict)
    path: Path

    def resolve_rule(self, action: str) -> PolicyRule | None:
        """Find the most specific matching rule for an action string."""

        for rule in self.rules:
            if fnmatch(action, rule.action):
                return rule
        return None

    def mode_for(self, action: str) -> PolicyMode:
        """Return the effective policy mode, falling back to deny-by-default."""

        rule = self.resolve_rule(action)
        if rule is None:
            return self.default_mode
        return rule.mode


class WorkflowDefinition(BaseModel):
    """Workflow wiring loaded from versioned files under `workflows/`."""

    workflow_id: str
    version: str
    description: str
    worker: str
    connector: str
    connector_config: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    policy: str
    sample_source: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[WorkflowToolDefinition] = Field(default_factory=list)
    path: Path


class WorkflowToolDefinition(BaseModel):
    """One tool step declared inside a workflow definition."""

    tool_id: str
    kind: str
    enabled: bool = True
    prompt: str | None = None
    expected_sha256: str | None = None
    provider: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_known_tool_kind(cls, value: str) -> str:
        from app.shared.tool_registry import get_tool_spec

        normalized = value.strip()
        if not normalized:
            raise ValueError("Workflow tool kind must be a non-empty string.")
        get_tool_spec(normalized)
        return normalized


class RunRecord(BaseModel):
    """Structured summary row for SQLite-backed run tracking."""

    run_id: str
    workflow_id: str
    status: RunStatus
    started_at: datetime
    updated_at: datetime
    summary: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    """Append-only event written to the JSONL audit trail."""

    event_id: str
    run_id: str
    workflow_id: str
    timestamp: datetime
    actor: str
    component: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = True
