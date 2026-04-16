"""Models for the starter newsletter-identification workflows."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import (
    EmailClassification,
    EmailMessage,
    EmailThreadTagResult,
    EmailTriageArtifact,
)


class NewsletterIdentifierItem(BaseModel):
    """One message-level result in the starter newsletter workflow."""

    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    classification: EmailClassification
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)
    tag_result: EmailThreadTagResult | None = None


class NewsletterIdentifierResult(BaseModel):
    """Aggregate starter newsletter workflow result."""

    model_config = ConfigDict(extra="forbid")

    reviewed_message_count: int = 0
    classified_message_count: int = 0
    newsletter_message_count: int = 0
    tagged_thread_count: int = 0
    items: list[NewsletterIdentifierItem] = Field(default_factory=list)


def build_newsletter_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    items: list[NewsletterIdentifierItem],
) -> EmailTriageArtifact:
    return EmailTriageArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        items=[item.classification for item in items],
    )
