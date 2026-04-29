"""Schemas for Gmail taxonomy-label cleanup runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailMessage

LabelCleanupStatus = Literal["cleared_partial_labels", "kept_complete_labels"]


class LabelCleanupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    current_label_names: list[str] = Field(default_factory=list)
    removed_label_names: list[str] = Field(default_factory=list)
    removed_label_ids: list[str] = Field(default_factory=list)
    status: LabelCleanupStatus
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class LabelCleanupArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_thread_ids: list[str] = Field(default_factory=list)
    cleaned_thread_ids: list[str] = Field(default_factory=list)
    items: list[LabelCleanupItem] = Field(default_factory=list)


def build_label_cleanup_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    items: list[LabelCleanupItem],
) -> LabelCleanupArtifact:
    return LabelCleanupArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_thread_ids=[
            item.message.thread_id or item.message.message_id
            for item in items
        ],
        cleaned_thread_ids=[
            item.message.thread_id or item.message.message_id
            for item in items
            if item.status == "cleared_partial_labels"
        ],
        items=items,
    )
