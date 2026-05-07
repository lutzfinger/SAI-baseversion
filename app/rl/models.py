"""Core data models for the RL-from-human-feedback layer."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RewardSource(StrEnum):
    """Where the reward signal came from."""

    APPROVAL_APPROVED = "approval_approved"   # ApprovalStatus.APPROVED
    APPROVAL_DENIED   = "approval_denied"     # ApprovalStatus.DENIED
    SLACK_POSITIVE    = "slack_positive"      # ✅ reaction or approve action
    SLACK_NEGATIVE    = "slack_negative"      # ❌ reaction or deny action
    SLACK_EDIT        = "slack_edit"          # human edited the output
    EVAL_GROUND_TRUTH = "eval_ground_truth"   # reality matched active_decision
    EVAL_MISMATCH     = "eval_mismatch"       # reality contradicted active_decision
    ABSTAIN           = "abstain"             # no usable signal; exclude from training


REWARD_SCALARS: dict[RewardSource, float] = {
    RewardSource.APPROVAL_APPROVED: +1.0,
    RewardSource.APPROVAL_DENIED:   -1.0,
    RewardSource.SLACK_POSITIVE:    +1.0,
    RewardSource.SLACK_NEGATIVE:    -1.0,
    RewardSource.SLACK_EDIT:        +0.3,   # right intent, wrong execution
    RewardSource.EVAL_GROUND_TRUTH: +1.0,
    RewardSource.EVAL_MISMATCH:     -0.8,
    RewardSource.ABSTAIN:            0.0,
}


class TrajectoryStep(BaseModel):
    """One tool call within a trajectory — stored untruncated."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    args: str
    result: str
    at: datetime
    latency_ms: int = 0
    error: str | None = None


class RewardSignal(BaseModel):
    """Scalar reward derived from one human feedback event."""

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    source: RewardSource
    scalar: float
    decided_by: str | None = None
    decided_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoredTrajectory(BaseModel):
    """Complete RL training example: trajectory + human reward.

    Analogous to Hermes's ScoredDataGroup. One of these is produced
    per agent run that received a non-ABSTAIN human signal.
    """

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    invocation_id: str
    run_id: str | None = None
    record_id: str | None = None
    workflow_id: str
    task_id: str | None = None

    system_prompt: str
    user_message: str
    steps: list[TrajectoryStep]
    final_response: str
    model_used: str
    cost_usd: float
    started_at: datetime
    completed_at: datetime
    terminated_reason: str

    reward: float
    reward_source: RewardSource
    human_actor: str | None = None
    scored_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreferencePair(BaseModel):
    """One DPO training example: chosen vs rejected response for the same prompt."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str = Field(default_factory=lambda: str(uuid4()))
    workflow_id: str
    system_prompt: str
    prompt: str
    chosen_response: str
    rejected_response: str
    chosen_reward: float
    rejected_reward: float
    chosen_trajectory_id: str
    rejected_trajectory_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
