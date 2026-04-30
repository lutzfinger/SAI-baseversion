"""TieredTaskRunner — sequential cascade with early-stop, eval-record-writing.

Given a Task and an input, the runner:

  1. Walks tiers from cheapest to active_tier (in `task.tiers_up_to_active()`).
  2. Calls each tier's `predict()` in order.
  3. The FIRST tier that returns a non-abstaining Prediction whose confidence
     clears its `confidence_threshold` resolves the request. Cascade stops.
  4. If every tier through active_tier_id abstained, escalation_policy decides:
       - ASK_HUMAN  → invoke escalation tiers (typically HumanTier) once
       - USE_ACTIVE → apply active_tier's prediction even if abstained
       - DROP       → raise CascadeAbstainedError
  5. Write an EvalRecord with: tier_predictions (only tiers that ran),
     escalation_chain, active_decision, decided_at, reality window end.
  6. Append the record to the EvalRecordStore and return it.

Bounded cost: in the typical case ONE tier runs (the cheapest, confident).
In the worst case, every configured tier through active runs once each plus
the human ask. No tier runs more than once per input.

The optional graduation experiment (TaskConfig.graduation_experiment) is the
deliberate exception: when configured, the candidate tier is also invoked
on a sampled fraction of inputs for shadow comparison. Its predictions land
in `tier_predictions` but do NOT short-circuit the cascade — only the active
chain decides the outcome. Graduation review reads these later.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.eval.record import EvalRecord, Prediction
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack.task import EscalationPolicy, Task
from app.runtime.ai_stack.tier import Tier, is_resolved


class CascadeAbstainedError(RuntimeError):
    """Raised when escalation_policy=DROP and every tier through active abstained."""

    def __init__(self, *, task_id: str, input_id: str) -> None:
        super().__init__(
            f"All cascade tiers abstained for task={task_id!r} input={input_id!r}"
        )
        self.task_id = task_id
        self.input_id = input_id


class TieredTaskRunner:
    """Run one Task input through the cascade, write the EvalRecord."""

    def __init__(
        self,
        *,
        eval_store: EvalRecordStore,
        clock: Any = None,                 # callable() -> datetime; testing seam
        sampler: Any = None,               # callable() -> float in [0,1); testing seam
    ) -> None:
        self.eval_store = eval_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sampler = sampler or random.random

    def run(
        self,
        task: Task,
        *,
        input_id: str,
        input_data: dict[str, Any],
    ) -> EvalRecord:
        decided_at = self._clock()
        cascade = task.tiers_up_to_active()
        tier_predictions: dict[str, Prediction] = {}
        escalation_chain: list[str] = []

        # Optional shadow run: candidate tier of a graduation experiment.
        candidate_pred = self._maybe_shadow_candidate(task=task, input_data=input_data)
        if candidate_pred is not None:
            tier_predictions[candidate_pred.tier_id] = candidate_pred
            # Note: shadow predictions don't enter escalation_chain — they're
            # for graduation review, not for resolving this request.

        resolved_by: Tier | None = None
        for tier in cascade:
            prediction = tier.predict(input_data)
            tier_predictions[tier.tier_id] = prediction
            escalation_chain.append(tier.tier_id)
            if is_resolved(prediction, threshold=tier.confidence_threshold):
                resolved_by = tier
                break

        active_decision: dict[str, Any]
        ask_id: str | None = None
        if resolved_by is not None:
            active_decision = tier_predictions[resolved_by.tier_id].output
        else:
            active_decision, ask_id = self._handle_abstain(
                task=task,
                tier_predictions=tier_predictions,
                escalation_chain=escalation_chain,
                input_id=input_id,
                input_data=input_data,
            )

        record = EvalRecord(
            record_id=str(uuid4()),
            task_id=task.task_id,
            input_id=input_id,
            input=input_data,
            escalation_chain=escalation_chain,
            tier_predictions=tier_predictions,
            active_decision=active_decision,
            decided_at=decided_at,
            reality_observation_window_ends_at=decided_at + task.reality_window,
        )
        if ask_id is not None:
            # link_ask sets ask_id AND flips reality_status to ASKED.
            record.link_ask(ask_id)
        self.eval_store.append(record)
        return record

    def _handle_abstain(
        self,
        *,
        task: Task,
        tier_predictions: dict[str, Prediction],
        escalation_chain: list[str],
        input_id: str,
        input_data: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        policy = task.config.escalation_policy
        if policy == EscalationPolicy.DROP:
            raise CascadeAbstainedError(task_id=task.task_id, input_id=input_id)

        if policy == EscalationPolicy.USE_ACTIVE:
            active_id = task.config.active_tier_id
            return tier_predictions[active_id].output, None

        # policy == ASK_HUMAN
        for tier in task.escalation_tiers():
            prediction = tier.predict(input_data)
            tier_predictions[tier.tier_id] = prediction
            escalation_chain.append(tier.tier_id)
            ask_id = prediction.metadata.get("ask_id")
            if ask_id:
                return prediction.output, str(ask_id)
            if is_resolved(prediction, threshold=tier.confidence_threshold):
                return prediction.output, None

        # No human tier configured — fall back to active output even if abstained.
        active_id = task.config.active_tier_id
        return tier_predictions[active_id].output, None

    def _maybe_shadow_candidate(
        self,
        *,
        task: Task,
        input_data: dict[str, Any],
    ) -> Prediction | None:
        """Run the graduation candidate (if configured + sample roll succeeds)."""

        experiment = task.config.graduation_experiment
        if experiment is None:
            return None
        if self._sampler() >= experiment.sample_rate:
            return None
        candidate = next(
            (t for t in task.tiers if t.tier_id == experiment.candidate_tier_id),
            None,
        )
        if candidate is None:
            return None
        return candidate.predict(input_data)
