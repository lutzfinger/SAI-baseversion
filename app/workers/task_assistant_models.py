"""Minimal propose-first execution-plan schemas for the starter repo."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord

TaskActionKind = Literal["run_workflow", "post_slack_message"]
TaskRiskLevel = Literal["low", "moderate", "high"]
TaskExecutionStatus = Literal["completed", "skipped", "failed"]


class TaskAction(BaseModel):
    """One approval-backed action in a starter execution plan."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    action_kind: TaskActionKind
    purpose: str
    workflow_id: str | None = None
    connector_overrides: dict[str, Any] = Field(default_factory=dict)
    channel: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> TaskAction:
        if self.action_kind == "run_workflow" and not self.workflow_id:
            raise ValueError("run_workflow actions require workflow_id")
        if self.action_kind == "post_slack_message" and (not self.channel or not self.text):
            raise ValueError("post_slack_message actions require channel and text")
        return self


class TaskExecutionPlan(BaseModel):
    """Structured plan proposed before any write-side effect happens."""

    model_config = ConfigDict(extra="forbid")

    task_summary: str
    approach_summary: str
    operator_approval_question: str
    requires_approval: bool = True
    risk_level: TaskRiskLevel
    actions: list[TaskAction] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    safety_notes: list[str] = Field(default_factory=list)


class TaskExecutionStepResult(BaseModel):
    """One executed step after approval."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    action_kind: TaskActionKind
    status: TaskExecutionStatus
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class TaskExecutionOutcome(BaseModel):
    """Execution summary for one approved plan."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    approved_by: str
    approved_at: datetime
    step_results: list[TaskExecutionStepResult] = Field(default_factory=list)
    completed_action_count: int = 0
    failed_action_count: int = 0


class TaskAssistantArtifact(BaseModel):
    """Persisted artifact for one starter task suggestion."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    requested_by: str
    request_text: str
    context_lines: list[str] = Field(default_factory=list)
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    approval_request_id: str | None = None
    plan: TaskExecutionPlan
    execution: TaskExecutionOutcome | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


def build_task_assistant_artifact(
    *,
    run_id: str,
    workflow_id: str,
    requested_by: str,
    request_text: str,
    context_lines: list[str],
    prompts: list[PromptDocument],
    plan: TaskExecutionPlan,
    tool_records: list[ToolExecutionRecord],
    approval_request_id: str | None = None,
    execution: TaskExecutionOutcome | None = None,
) -> TaskAssistantArtifact:
    return TaskAssistantArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        requested_by=requested_by,
        request_text=request_text,
        context_lines=context_lines,
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        approval_request_id=approval_request_id,
        plan=plan,
        execution=execution,
        tool_records=tool_records,
    )
