"""Models for the deterministic newsletter lane router."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import (
    EmailClassification,
    EmailMessage,
    GmailThreadLabelResult,
)

NewsletterLaneStatus = Literal[
    "kept_newsletter",
    "remove_newsletter",
    "not_newsletter",
]


class NewsletterLaneItem(BaseModel):
    """Outcome for one message considered by the newsletter lane."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    email_classification: EmailClassification
    status: NewsletterLaneStatus
    route: Literal["keep", "remove", "skip"]
    reason: str
    label_result: GmailThreadLabelResult | None = None
    unsubscribe_status: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class NewsletterLaneArtifact(BaseModel):
    """Persisted artifact for deterministic newsletter routing runs."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    reviewed_message_ids: list[str] = Field(default_factory=list)
    items: list[NewsletterLaneItem] = Field(default_factory=list)


def build_newsletter_lane_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    reviewed_message_ids: list[str],
    items: list[NewsletterLaneItem],
) -> NewsletterLaneArtifact:
    return NewsletterLaneArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        reviewed_message_ids=reviewed_message_ids,
        items=items,
    )
