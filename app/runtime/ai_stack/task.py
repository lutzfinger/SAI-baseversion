"""Task — runtime + static config for one cascade-driven workflow.

A Task pairs a list of live Tier instances (constructed in Python) with a
TaskConfig (loaded from YAML). The TaskConfig holds the parameters the
graduation reviewer and reality reconciler care about: task_id, active_tier,
reconciliation window, graduation thresholds, escalation policy. Tier
instantiation is left to user code (private overlays usually) because Tiers
need callables, providers, and prompt renderers that don't round-trip through
YAML.

Layout on disk:

  registry/tasks/<task_id>.yaml      — TaskConfig (this module loads it)
  app/<your_overlay>/tasks/<task_id>.py — code that loads the YAML and
                                         instantiates the Tier list

The runner (runner.py) consumes the live Task object.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.runtime.ai_stack.tier import Tier, TierKind


class EscalationPolicy(StrEnum):
    """What to do when the active tier abstains."""

    ASK_HUMAN = "ask_human"          # cascade enters HumanTier (default)
    USE_ACTIVE = "use_active"        # apply active_tier's output even if abstained
    DROP = "drop"                    # raise — caller decides


class GraduationThresholds(BaseModel):
    """Per-tier-kind precision/recall bars to clear for graduation."""

    model_config = ConfigDict(extra="forbid")

    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)


class GraduationExperimentConfig(BaseModel):
    """Time-bounded, sample-rate-bounded shadow run of a candidate tier.

    Per the cost-conscious design: don't shadow on every input, ever — only
    during a deliberate experiment that the GraduationReviewer will read.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_tier_id: str
    sample_rate: float = Field(ge=0.0, le=1.0)  # fraction of inputs to also run candidate
    trigger: str | None = None                  # optional filter, e.g. "tier_kind == cloud_llm"
    starts_at: str | None = None                # ISO date; None = active immediately
    ends_at: str | None = None                  # ISO date; None = no end


class TaskConfig(BaseModel):
    """Static config for one Task, loaded from registry/tasks/<task_id>.yaml."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    description: str
    input_schema_class: str                     # dotted path; loaded by user code
    output_schema_class: str                    # dotted path

    # The cascade ceiling: which tier_id is the most expensive we'll go to
    # in normal operation. Tiers above this in the configured tier list are
    # only used if escalation_policy = ASK_HUMAN and active abstains.
    active_tier_id: str

    escalation_policy: EscalationPolicy = EscalationPolicy.ASK_HUMAN
    reality_observation_window_days: int = 7

    graduation_thresholds: dict[TierKind, GraduationThresholds] = Field(
        default_factory=dict,
    )
    graduation_experiment: GraduationExperimentConfig | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> TaskConfig:
        """Load a TaskConfig from a YAML file."""

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)


class Task:
    """Live Task: TaskConfig + ordered Tier instances + helpers.

    `tiers` must be ordered cheapest → most expensive. The cascade halts at
    `active_tier_id`; tiers after that in the list are reserved for
    escalation (typically the HumanTier). If escalation_policy is USE_ACTIVE
    or DROP, tiers after active_tier_id are never invoked even on abstain.
    """

    def __init__(
        self,
        *,
        config: TaskConfig,
        tiers: list[Tier],
    ) -> None:
        self.config = config
        self.tiers = tiers
        self._tier_index = {tier.tier_id: index for index, tier in enumerate(tiers)}
        if config.active_tier_id not in self._tier_index:
            raise ValueError(
                f"active_tier_id={config.active_tier_id!r} not in tiers "
                f"{list(self._tier_index)!r}"
            )

    @property
    def task_id(self) -> str:
        return self.config.task_id

    @property
    def reality_window(self) -> timedelta:
        return timedelta(days=self.config.reality_observation_window_days)

    def tiers_up_to_active(self) -> list[Tier]:
        """Tiers from cheapest through active_tier_id (inclusive). Cascade walks these."""

        end = self._tier_index[self.config.active_tier_id] + 1
        return self.tiers[:end]

    def escalation_tiers(self) -> list[Tier]:
        """Tiers AFTER active_tier_id; only used when policy is ASK_HUMAN."""

        start = self._tier_index[self.config.active_tier_id] + 1
        return self.tiers[start:]
