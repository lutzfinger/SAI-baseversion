"""Schemas for weekly people-of-interest monitoring."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord


class PersonOfInterestFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    source_url: str
    source_title: str | None = None
    event_date: str | None = None


class PersonOfInterestSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_query: str
    overall_summary: str
    no_notable_updates: bool = False
    identity_confidence: str = "high"
    findings: list[PersonOfInterestFinding] = Field(default_factory=list)


class PersonOfInterestReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: str
    display_name: str
    canonical_url: str
    organization: str | None = None
    status: str
    report: PersonOfInterestSearchResponse | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class PeopleOfInterestReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    people_count: int = 0
    searched_count: int = 0
    updated_count: int = 0
    failed_count: int = 0
    slack_channel: str | None = None
    slack_ts: str | None = None
    items: list[PersonOfInterestReviewItem] = Field(default_factory=list)
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class PeopleOfInterestReviewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: PeopleOfInterestReviewResult


def build_people_of_interest_review_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: PeopleOfInterestReviewResult,
) -> PeopleOfInterestReviewArtifact:
    return PeopleOfInterestReviewArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
