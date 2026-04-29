"""Schemas for the meeting-decision workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailClassification, EmailMessage

MeetingRequestType = Literal[
    "conference_talk",
    "investment_pitch",
    "job_or_recruiting",
    "networking_intro",
    "partnership_or_sales",
    "follow_up",
    "unknown",
]
MeetingAction = Literal[
    "send_calendar_link",
    "ask_for_more_info",
    "manual_review",
]


class MeetingEvidence(BaseModel):
    """Structured enrichment collected before scoring one meeting request."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    contact_email: str
    contact_name: str | None = None
    gmail_history: dict[str, Any] = Field(default_factory=dict)
    calendar_history: dict[str, Any] = Field(default_factory=dict)
    meetings_in_last_12_months: int = 0
    met_before_in_last_12_months: bool = False
    last_meeting_at: datetime | None = None
    linkedin: dict[str, Any] = Field(default_factory=dict)
    request_type: MeetingRequestType


class MeetingLikelihoodAssessment(BaseModel):
    """Decision produced by the Phase 1 likelihood predictor."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    request_type: MeetingRequestType
    decision: MeetingAction
    likelihood_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    should_create_draft: bool = True


class CalendarMeetingHistoryResult(BaseModel):
    """Strict JSON summary of whether and how often Lutz met this contact."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    contact_email: str
    contact_name: str | None = None
    lookback_days: int = 365
    meetings_in_last_12_months: int = 0
    upcoming_meeting_count: int = 0
    has_met_in_last_12_months: bool = False
    last_meeting_at: datetime | None = None


class MeetingDraftPackage(BaseModel):
    """Operator-facing explanation plus the external reply draft."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    internal_note: str
    external_subject: str
    external_body: str
    draft_created: bool = False
    draft_id: str | None = None


class MeetingDecisionItem(BaseModel):
    """One fully processed meeting decision artifact item."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    email_classification: EmailClassification
    evidence: MeetingEvidence
    assessment: MeetingLikelihoodAssessment
    draft: MeetingDraftPackage
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class MeetingDecisionArtifact(BaseModel):
    """Persisted JSON artifact for the meeting-decision workflow."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_message_ids: list[str] = Field(default_factory=list)
    candidate_message_ids: list[str] = Field(default_factory=list)
    drafted_message_ids: list[str] = Field(default_factory=list)
    items: list[MeetingDecisionItem] = Field(default_factory=list)


def build_meeting_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    reviewed_message_ids: list[str],
    items: list[MeetingDecisionItem],
) -> MeetingDecisionArtifact:
    return MeetingDecisionArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_message_ids=reviewed_message_ids,
        candidate_message_ids=[item.message.message_id for item in items],
        drafted_message_ids=[
            item.message.message_id for item in items if item.draft.draft_created
        ],
        items=items,
    )
