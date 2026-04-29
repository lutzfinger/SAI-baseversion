"""Schemas for the Gmail manual label-correction review workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import (
    EmailClassification,
    EmailMessage,
    Level1Classification,
    Level2Intent,
)

ManualLabelCorrectionReviewStatus = Literal[
    "recorded_correction",
    "duplicate_correction",
    "skipped_unchanged",
    "skipped_no_taxonomy_labels",
    "skipped_no_source_example",
    "skipped_ambiguous_taxonomy_labels",
]


class ManualLabelCorrectionReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    current_label_names: list[str] = Field(default_factory=list)
    extracted_level1_classification: Level1Classification | None = None
    extracted_level2_intent: Level2Intent | None = None
    prior_final_classification: EmailClassification | None = None
    training_record_id: str | None = None
    duplicates_skipped: int = 0
    status: ManualLabelCorrectionReviewStatus
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class ManualLabelCorrectionReviewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    items: list[ManualLabelCorrectionReviewItem] = Field(default_factory=list)


def build_manual_label_correction_review_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    items: list[ManualLabelCorrectionReviewItem],
) -> ManualLabelCorrectionReviewArtifact:
    return ManualLabelCorrectionReviewArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        items=items,
    )
