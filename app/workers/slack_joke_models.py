"""Schemas for the guarded Slack joke workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.tools.safe_joke_writer import SafeJokeResponse

SlackJokeStatus = Literal[
    "completed",
    "completed_with_fallback",
    "completed_with_guarded_fallback",
    "failed",
]


class SlackJokeDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    thread_ts: str | None = None
    text: str
    posted: bool = False
    ts: str | None = None


class SlackJokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_text: str
    sanitized_request_text: str | None = None
    status: SlackJokeStatus
    input_guard_blocked: bool = False
    used_canned_fallback: bool = False
    joke: SafeJokeResponse | None = None
    delivery: SlackJokeDelivery | None = None
    error: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class SlackJokeArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: SlackJokeResult


def build_slack_joke_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: SlackJokeResult,
) -> SlackJokeArtifact:
    return SlackJokeArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )

