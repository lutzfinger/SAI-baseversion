"""Schemas for deeper contact investigation on ambiguous personal emails."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailClassification, EmailMessage, Level1Classification

KnownRelationship = Literal[
    "unknown",
    "known_contact",
    "known_professional",
    "known_personal",
    "known_friend",
    "met_before",
    "linkedin_connection",
]


class ContactInvestigationAssessment(BaseModel):
    """Strict result of deeper relationship analysis for one email."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    original_level1_classification: Level1Classification
    suggested_level1_classification: Level1Classification
    category_updated: bool
    known_contact: bool
    known_relationship: KnownRelationship
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    evidence_sources: list[str] = Field(default_factory=list)


class ContactInvestigationDraftPackage(BaseModel):
    """Internal Gmail draft that summarizes deeper findings for Lutz."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    to_email: str
    draft_subject: str
    draft_body: str
    thread_id: str | None = None
    draft_created: bool = False
    draft_id: str | None = None


class ContactInvestigationItem(BaseModel):
    """One investigated email plus the enrichment and resulting draft."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    email_classification: EmailClassification
    gmail_history: dict[str, object] = Field(default_factory=dict)
    calendar_history: dict[str, object] = Field(default_factory=dict)
    linkedin: dict[str, object] = Field(default_factory=dict)
    assessment: ContactInvestigationAssessment
    draft: ContactInvestigationDraftPackage
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class ContactInvestigationArtifact(BaseModel):
    """Persisted JSON artifact for the contact-investigation workflow."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_message_ids: list[str] = Field(default_factory=list)
    investigated_message_ids: list[str] = Field(default_factory=list)
    drafted_message_ids: list[str] = Field(default_factory=list)
    items: list[ContactInvestigationItem] = Field(default_factory=list)


def build_contact_investigation_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    reviewed_message_ids: list[str],
    items: list[ContactInvestigationItem],
) -> ContactInvestigationArtifact:
    return ContactInvestigationArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_message_ids=reviewed_message_ids,
        investigated_message_ids=[item.message.message_id for item in items],
        drafted_message_ids=[
            item.message.message_id for item in items if item.draft.draft_created
        ],
        items=items,
    )
