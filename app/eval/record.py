"""Per-input EvalRecord — the central artifact of the eval-centric architecture.

One EvalRecord is written for every task input the system processes. It captures:

  - The input itself
  - Which tiers actually ran (the cascade may have early-stopped)
  - The active decision the system applied
  - The reality reconciliation status (pending / observed / asked / answered / skipped)
  - The observed reality, when known — the ONLY source of ground truth

Tier predictions are *transient*: they live in the record for audit and graduation
review, but they are never ground truth. Ground truth comes only from the human's
real-world action, an explicit Slack-ask answer, or a co-work approval. The
`is_ground_truth` flag is sacred — only set when reality has been confirmed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RealityStatus(str, Enum):
    """Where this record sits in the reality-reconciliation lifecycle."""

    PENDING = "pending"          # decided; awaiting reality observation
    OBSERVED = "observed"        # reality found via passive observation (e.g., user re-tagged)
    ASKED = "asked"              # ambiguous → posted Slack ask, awaiting reply
    ANSWERED = "answered"        # human replied to our Slack ask
    SKIPPED = "skipped"          # ambiguous + ask budget hit; excluded from training


class RealitySource(str, Enum):
    """How we learned what reality actually was."""

    HUMAN_LABEL = "human_label"                    # explicit user label (Gmail label, etc.)
    HUMAN_ACTION = "human_action"                  # observed action (archive, reply, move)
    COWORK = "cowork"                              # extracted from a co-work session
    SLACK_ASK = "slack_ask"                        # explicit answer to a posted ask
    CALENDAR_OBSERVATION = "calendar_observation"  # event appeared / didn't appear
    BOOKING_CONFIRMATION = "booking_confirmation"  # external system confirmation
    OTHER = "other"


class Prediction(BaseModel):
    """One tier's prediction for one task input.

    A prediction is *always transient* — it never becomes ground truth, even if
    every tier agrees. Ground truth requires real-world observation or a human
    answer. See `EvalRecord.reality` for that.
    """

    model_config = ConfigDict(extra="forbid")

    tier_id: str
    output: dict[str, Any]                         # serialized; matches task output schema
    confidence: float = Field(ge=0.0, le=1.0)
    abstained: bool = False                        # tier explicitly said "I don't know"
    cost_usd: float = Field(default=0.0, ge=0.0)   # provider+model cost for this call
    latency_ms: int = Field(default=0, ge=0)
    reasoning: str | None = None                   # tier's explanation, optional
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservedReality(BaseModel):
    """Ground truth derived from real-world observation or human answer.

    When this is set on an EvalRecord, `is_ground_truth=True` follows. This is
    the only data point that contributes to graduation review (precision/recall)
    and to training cheaper tiers.
    """

    model_config = ConfigDict(extra="forbid")

    label: dict[str, Any]                          # the actual output observed
    source: RealitySource
    observed_at: datetime
    notes: str | None = None
    raw_signal: dict[str, Any] = Field(default_factory=dict)


class EvalRecord(BaseModel):
    """One processed task input plus everything we know (or learn) about it."""

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    input_id: str                                  # task-specific (message_id, booking_id, ...)
    input: dict[str, Any]                          # serialized; matches task input schema

    # What ran. The cascade is sequential with early-stop, so most records
    # have a short escalation_chain (length 1–2). The last entry is the
    # tier whose prediction became `active_decision`. Tiers that abstained
    # appear earlier in the chain.
    escalation_chain: list[str] = Field(default_factory=list)
    tier_predictions: dict[str, Prediction] = Field(default_factory=dict)

    active_decision: dict[str, Any]                # what the system did
    decided_at: datetime

    # Reality reconciliation:
    reality_status: RealityStatus = RealityStatus.PENDING
    reality: ObservedReality | None = None
    reality_observation_window_ends_at: datetime | None = None
    ask_id: str | None = None                      # link to a Slack Ask record

    # Eval gate. The single flag the graduation reviewer trusts.
    is_ground_truth: bool = False

    metadata: dict[str, Any] = Field(default_factory=dict)

    def record_reality(self, reality: ObservedReality) -> None:
        """Set ground truth based on a reality observation or Slack answer."""

        self.reality = reality
        if reality.source == RealitySource.SLACK_ASK:
            self.reality_status = RealityStatus.ANSWERED
        else:
            self.reality_status = RealityStatus.OBSERVED
        self.is_ground_truth = True

    def mark_skipped(self, reason: str | None = None) -> None:
        """Mark this record as skipped: ambiguous reality, no ask budget.

        The record stays in audit but never enters training or graduation P/R.
        """

        self.reality_status = RealityStatus.SKIPPED
        if reason:
            self.metadata["skip_reason"] = reason

    def link_ask(self, ask_id: str) -> None:
        """Mark this record as having a posted Slack ask awaiting reply."""

        self.ask_id = ask_id
        self.reality_status = RealityStatus.ASKED
