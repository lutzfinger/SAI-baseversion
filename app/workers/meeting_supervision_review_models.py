"""Schemas for daily reconciliation of supervision ledger vs Gmail labels."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord

MeetingSupervisionMismatchKind = Literal["missing_label", "unexpected_label"]


class MeetingSupervisionMismatchItem(BaseModel):
    """One thread where the supervision ledger and Gmail label disagree."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    subject: str
    mismatch_kind: MeetingSupervisionMismatchKind
    expected_supervising: bool
    actual_supervising: bool
    reason: str
    source_workflow_id: str | None = None
    target_contact_email: str | None = None
    followup_due_at: datetime | None = None
    source_run_id: str | None = None
    updated_at: datetime | None = None


class MeetingSupervisionReviewSlackDelivery(BaseModel):
    """Slack delivery details for a mismatch alert."""

    model_config = ConfigDict(extra="forbid")

    posted: bool
    channel: str | None = None
    ts: str | None = None
    text: str | None = None


class MeetingSupervisionReviewResult(BaseModel):
    """Outcome of reconciling the supervision ledger against Gmail labels."""

    model_config = ConfigDict(extra="forbid")

    reviewed_at: datetime
    ledger_thread_count: int
    labeled_thread_count: int
    expected_supervising_count: int
    mismatch_count: int
    items: list[MeetingSupervisionMismatchItem] = Field(default_factory=list)
    slack: MeetingSupervisionReviewSlackDelivery | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class MeetingSupervisionReviewArtifact(BaseModel):
    """Persisted artifact for meeting supervision reconciliation runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: MeetingSupervisionReviewResult


def build_meeting_supervision_review_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: MeetingSupervisionReviewResult,
) -> MeetingSupervisionReviewArtifact:
    return MeetingSupervisionReviewArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
