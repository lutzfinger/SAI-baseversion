"""Schemas for daily token-usage reporting workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord

TokenUsageSnapshotStatus = Literal["actual", "unavailable", "failed"]


class TokenUsageTotalLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    token_count: int = Field(ge=0)


class AuditTokenUsageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    run_id: str
    workflow_id: str
    tool_id: str
    tool_kind: str
    total_tokens: int = Field(ge=1)
    provider: str | None = None


class LangSmithTokenUsageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str | None = None
    node_name: str
    path: str
    total_tokens: int = Field(ge=1)
    total_cost: float | None = Field(default=None, ge=0)


class AuditTokenUsageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TokenUsageSnapshotStatus
    source: str
    started_at: datetime
    ended_at: datetime
    entries: list[AuditTokenUsageEntry] = Field(default_factory=list)
    workflow_totals: list[TokenUsageTotalLine] = Field(default_factory=list)
    tool_totals: list[TokenUsageTotalLine] = Field(default_factory=list)
    total_tokens: int | None = Field(default=None, ge=0)
    note: str | None = None


class LangSmithTokenUsageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TokenUsageSnapshotStatus
    source: str
    started_at: datetime
    ended_at: datetime
    entries: list[LangSmithTokenUsageEntry] = Field(default_factory=list)
    total_tokens: int | None = Field(default=None, ge=0)
    root_run_count: int = Field(default=0, ge=0)
    note: str | None = None


class DailyTokenUsageSlackDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    text: str
    posted: bool = False
    ts: str | None = None


class DailyTokenUsageReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_label: str
    window_display_label: str
    timezone_name: str
    timezone_abbreviation: str
    audit: AuditTokenUsageSnapshot
    langsmith: LangSmithTokenUsageSnapshot
    langsmith_only_totals: list[TokenUsageTotalLine] = Field(default_factory=list)
    slack: DailyTokenUsageSlackDelivery | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class DailyTokenUsageReportArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: DailyTokenUsageReportResult


def build_daily_token_usage_report_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: DailyTokenUsageReportResult,
) -> DailyTokenUsageReportArtifact:
    return DailyTokenUsageReportArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
