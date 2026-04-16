"""Schemas for the starter email-native interaction workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.shared.models import PromptDocument
from app.shared.registry import validate_task_kind_name
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailMessage
from app.workers.task_assistant_models import TaskExecutionPlan

SaiEmailResponseMode = Literal[
    "ask_information", "ask_approval", "answer_only", "completed", "failed"
]
SaiEmailThreadStatus = Literal[
    "awaiting_information",
    "awaiting_approval",
    "completed",
    "failed",
]


class SaiEmailActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str
    activity_kind: str
    description: str
    approval_required: bool = False


class SaiEmailGenericPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_mode: SaiEmailResponseMode
    short_response: str
    explanation: str
    activities: list[SaiEmailActivity] = Field(default_factory=list)
    request_kind: str = "workflow_suggestion"
    execution_plan: TaskExecutionPlan | None = None
    follow_up_question: str | None = None

    @field_validator("request_kind")
    @classmethod
    def validate_request_kind(cls, value: str) -> str:
        normalized = validate_task_kind_name(value)
        if normalized is None:
            raise ValueError("request_kind must be a valid task kind.")
        return normalized

    @model_validator(mode="after")
    def validate_shape(self) -> SaiEmailGenericPlan:
        if self.response_mode == "ask_information" and not self.follow_up_question:
            raise ValueError("ask_information responses require follow_up_question")
        if self.response_mode == "ask_approval" and self.execution_plan is None:
            raise ValueError("ask_approval responses require execution_plan")
        return self


class SaiEmailThreadState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    task_id: str
    request_kind: str
    status: SaiEmailThreadStatus
    request_message_id: str
    request_subject: str | None = None
    approval_request_id: str | None = None
    current_plan: dict[str, Any] | None = None
    pending_question: str | None = None
    short_response: str | None = None
    explanation: str | None = None
    last_processed_message_id: str | None = None
    last_response_message_id: str | None = None
    reply_recipient_email: str | None = None
    activity_ids: list[str] = Field(default_factory=list)

    @field_validator("request_kind")
    @classmethod
    def validate_request_kind(cls, value: str) -> str:
        normalized = validate_task_kind_name(value)
        if normalized is None:
            raise ValueError("request_kind must be a valid task kind.")
        return normalized


class SaiEmailInteractionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_message: EmailMessage
    request_kind: str
    response_mode: SaiEmailResponseMode
    short_response: str
    explanation: str
    activities: list[SaiEmailActivity] = Field(default_factory=list)
    approval_request_id: str | None = None
    response_message_id: str | None = None
    thread_state: SaiEmailThreadState | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)
    error: str | None = None


class SaiEmailInteractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_thread_count: int = 0
    replied_count: int = 0
    awaiting_information_count: int = 0
    awaiting_approval_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    items: list[SaiEmailInteractionItem] = Field(default_factory=list)


class SaiEmailInteractionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_thread_ids: list[str] = Field(default_factory=list)
    result: SaiEmailInteractionResult


def build_sai_email_interaction_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    reviewed_thread_ids: list[str],
    result: SaiEmailInteractionResult,
) -> SaiEmailInteractionArtifact:
    return SaiEmailInteractionArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_thread_ids=reviewed_thread_ids,
        result=result,
    )
