"""Schemas for daily provider cost reporting workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord

ProviderCostStatus = Literal["actual", "unavailable", "failed"]


class ProviderCostLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    amount_usd: float = Field(ge=0)
    currency: str = "USD"


class DailyProviderCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "gemini"]
    status: ProviderCostStatus
    source: str
    started_at: datetime
    ended_at: datetime
    amount_usd: float | None = Field(default=None, ge=0)
    currency: str | None = None
    line_items: list[ProviderCostLineItem] = Field(default_factory=list)
    note: str | None = None


class DailyCostSlackDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    text: str
    posted: bool = False
    ts: str | None = None


class DailyCostReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_label: str
    timezone_name: str
    openai: DailyProviderCost
    gemini: DailyProviderCost
    slack: DailyCostSlackDelivery | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class DailyCostReportArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    result: DailyCostReportResult


def build_daily_cost_report_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None,
    result: DailyCostReportResult,
) -> DailyCostReportArtifact:
    return DailyCostReportArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        result=result,
    )
