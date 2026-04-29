"""Schemas for operator-approved deterministic email keyword routes."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import Level1Classification
from app.learning.email_keyword_routes import KeywordRouteMatchScope


class KeywordRouteIntakeResult(BaseModel):
    """Outcome of one operator-approved deterministic keyword route."""

    model_config = ConfigDict(extra="forbid")

    message_reference: str
    level1_classification: Level1Classification
    keyword_route_match_scope: KeywordRouteMatchScope
    keyword_route_match_value: str
    source_thread_id: str | None = None
    source_subject: str | None = None
    recorded: bool = True
    duplicates_skipped: int = 0
    label_applied: bool = False
    applied_label_names: list[str] = Field(default_factory=list)
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class KeywordRouteIntakeArtifact(BaseModel):
    """Persisted artifact for one keyword-route intake workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: KeywordRouteIntakeResult


def build_keyword_route_intake_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: KeywordRouteIntakeResult,
) -> KeywordRouteIntakeArtifact:
    return KeywordRouteIntakeArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
