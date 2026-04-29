"""Schemas for operator-approved prompt-and-dataset classification alignments."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailClassification, EmailMessage


class ClassificationAlignmentIntakeResult(BaseModel):
    """Outcome of one approved classification-alignment request."""

    model_config = ConfigDict(extra="forbid")

    message_reference: str
    matched_message: EmailMessage
    corrected_classification: EmailClassification
    training_record_id: str
    training_recorded: bool = True
    training_duplicates_skipped: int = 0
    alignment_rule_id: str
    alignment_recorded: bool = True
    alignment_duplicates_skipped: int = 0
    dataset_entry_id: str
    dataset_recorded: bool = True
    dataset_duplicates_skipped: int = 0
    prompt_addendum_path: str
    prompt_addendum_sha256: str | None = None
    prompt_addendum_rule_count: int = 0
    correction_reason: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class ClassificationAlignmentIntakeArtifact(BaseModel):
    """Persisted artifact for one alignment workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: ClassificationAlignmentIntakeResult


def build_classification_alignment_intake_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: ClassificationAlignmentIntakeResult,
) -> ClassificationAlignmentIntakeArtifact:
    return ClassificationAlignmentIntakeArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
