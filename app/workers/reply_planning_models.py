"""Schemas for the reply-planning workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailClassification, EmailMessage

ReplyStatus = Literal[
    "draft_reply_for_approval",
    "needs_more_context",
    "no_reply_needed",
]
SuggestedToolName = Literal[
    "gmail_history",
    "calendar_history",
    "linkedin_lookup",
    "restricted_web",
    "manual_context_from_lutz",
    "none",
]


class ReplyToolSuggestion(BaseModel):
    """One tool or external input the planner thinks would help."""

    model_config = ConfigDict(extra="forbid")

    tool_name: SuggestedToolName
    why_needed: str


class ReplyPlan(BaseModel):
    """Strict structured reply-planning output for one email."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    reply_status: ReplyStatus
    reply_goal: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_reply_points: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    suggested_tools: list[ReplyToolSuggestion] = Field(default_factory=list)
    plan_steps: list[str] = Field(default_factory=list)


class ReplyPlanningDraftPackage(BaseModel):
    """Internal approval draft generated from a reply plan."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    to_email: str
    draft_subject: str
    draft_body: str
    thread_id: str | None = None
    draft_created: bool = False
    draft_id: str | None = None


class ReplyPlanningItem(BaseModel):
    """One email plus classification, planning result, and saved draft."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    email_classification: EmailClassification
    plan: ReplyPlan
    draft: ReplyPlanningDraftPackage
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class ReplyPlanningArtifact(BaseModel):
    """Persisted artifact for reply-planning runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_message_ids: list[str] = Field(default_factory=list)
    drafted_message_ids: list[str] = Field(default_factory=list)
    items: list[ReplyPlanningItem] = Field(default_factory=list)


def build_reply_planning_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    reviewed_message_ids: list[str],
    items: list[ReplyPlanningItem],
) -> ReplyPlanningArtifact:
    return ReplyPlanningArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_message_ids=reviewed_message_ids,
        drafted_message_ids=[
            item.message.message_id for item in items if item.draft.draft_created
        ],
        items=items,
    )
