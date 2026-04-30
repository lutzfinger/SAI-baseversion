"""AskOrchestrator — decide which pending records become Slack asks.

Runs daily (or on-demand) over PENDING EvalRecords for one task. For each
candidate, scores the value of asking the human:

  coverage_score      = 1.0 - min(1.0, ground_truth_in_bucket / coverage_target)
                        — undercovered buckets get higher priority
  disagreement_score  = (distinct outputs among tiers that ran) / max(1, n_tiers)
                        — high tier disagreement is a signal that asking helps
  priority            = coverage_weight * coverage_score
                      + disagreement_weight * disagreement_score

Then:

  - Records below `min_priority_threshold` are marked SKIPPED (they're
    indistinguishable from "not worth asking"; they stay in audit but
    don't enter training).
  - Records above the threshold are asked, in priority order, until the
    daily budget is exhausted (default 5 per task per day, hard ceiling).
  - The remainder beyond budget are left PENDING — they'll be re-evaluated
    on the next orchestrator run.

This is eval-coverage aware: as more buckets reach coverage_target, the
orchestrator naturally shifts to asking about the long tail and the
disagreement-heavy edge cases.

The "today" boundary for the budget is computed from the orchestrator's
clock (UTC). Tasks with their own cadence (e.g. weekly) override
daily_budget and the implied window via the optional `budget_window`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.eval.ask import AskStore
from app.eval.record import EvalRecord, RealityStatus
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack.tiers.human import AskPoster

BucketingFn = Callable[[EvalRecord], str | None]


class AskOrchestrator:
    """Decide which PENDING records become Slack asks today.

    Stateless apart from store / poster handles. Safe to instantiate per run.
    """

    def __init__(
        self,
        *,
        task_id: str,
        ask_poster: AskPoster,
        eval_store: EvalRecordStore,
        ask_store: AskStore,
        bucketing_fn: BucketingFn,
        daily_budget: int = 5,
        coverage_target: int = 100,
        coverage_weight: float = 0.5,
        disagreement_weight: float = 0.5,
        min_priority_threshold: float = 0.10,
        budget_window: timedelta = timedelta(days=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.task_id = task_id
        self.ask_poster = ask_poster
        self.eval_store = eval_store
        self.ask_store = ask_store
        self.bucketing_fn = bucketing_fn
        self.daily_budget = daily_budget
        self.coverage_target = coverage_target
        self.coverage_weight = coverage_weight
        self.disagreement_weight = disagreement_weight
        self.min_priority_threshold = min_priority_threshold
        self.budget_window = budget_window
        self._clock = clock or (lambda: datetime.now(UTC))

    def review_pending(self) -> dict[str, int]:
        """Score all pending candidates, ask top-priority within budget."""

        now = self._clock()
        records = _latest_records_per_input(
            self.eval_store.read_all(self.task_id)
        )
        candidates = [
            r
            for r in records
            if r.reality_status == RealityStatus.PENDING
            and r.ask_id is None
            and r.reality is None
        ]

        coverage = self._compute_coverage(records)
        scored: list[tuple[float, EvalRecord]] = []
        for record in candidates:
            bucket = self.bucketing_fn(record)
            if bucket is None:
                continue
            priority = self._score(record, bucket=bucket, coverage=coverage)
            scored.append((priority, record))
        scored.sort(key=lambda item: item[0], reverse=True)

        remaining = self.daily_budget - self._asks_within_budget_window(now=now)
        counts = {"asked": 0, "skipped": 0, "waited": 0, "below_threshold": 0}

        for priority, record in scored:
            if priority < self.min_priority_threshold:
                self._mark_skipped(record, reason="below_priority_threshold")
                counts["below_threshold"] += 1
                counts["skipped"] += 1
                continue
            if remaining <= 0:
                counts["waited"] += 1
                continue
            self._post_ask_for(record)
            counts["asked"] += 1
            remaining -= 1
        return counts

    # ─── scoring ──────────────────────────────────────────────────────

    def _score(
        self,
        record: EvalRecord,
        *,
        bucket: str,
        coverage: dict[str, int],
    ) -> float:
        coverage_count = coverage.get(bucket, 0)
        coverage_ratio = min(1.0, coverage_count / max(1, self.coverage_target))
        coverage_score = 1.0 - coverage_ratio
        disagreement = self._disagreement_score(record)
        return (
            self.coverage_weight * coverage_score
            + self.disagreement_weight * disagreement
        )

    @staticmethod
    def _disagreement_score(record: EvalRecord) -> float:
        """Fraction of tiers that produced distinct (non-abstain) outputs."""

        live_outputs: list[str] = []
        for prediction in record.tier_predictions.values():
            if prediction.abstained:
                continue
            try:
                serialized = _stable_dumps(prediction.output)
            except Exception:
                serialized = repr(prediction.output)
            live_outputs.append(serialized)
        if not live_outputs:
            return 0.0
        distinct = len(set(live_outputs))
        return min(1.0, (distinct - 1) / max(1, len(live_outputs)))

    def _compute_coverage(self, records: list[EvalRecord]) -> dict[str, int]:
        """Count ground-truth records per bucket."""

        counts: dict[str, int] = {}
        for record in records:
            if not record.is_ground_truth:
                continue
            bucket = self.bucketing_fn(record)
            if bucket is None:
                continue
            counts[bucket] = counts.get(bucket, 0) + 1
        return counts

    # ─── budget ───────────────────────────────────────────────────────

    def _asks_within_budget_window(self, *, now: datetime) -> int:
        """Number of asks (any status) posted within the budget_window."""

        window_start = now - self.budget_window
        return sum(
            1
            for ask in self.ask_store.latest_state(self.task_id).values()
            if ask.posted_at >= window_start
        )

    # ─── side effects ─────────────────────────────────────────────────

    def _mark_skipped(self, record: EvalRecord, *, reason: str) -> None:
        updated = record.model_copy(deep=True)
        updated.mark_skipped(reason=reason)
        self.eval_store.append(updated)

    def _post_ask_for(self, record: EvalRecord) -> None:
        ask_id = self.ask_poster.post_ask(
            task_id=self.task_id,
            input_data=record.input,
            prior_predictions={
                tier_id: pred.model_dump(mode="json")
                for tier_id, pred in record.tier_predictions.items()
            },
        )
        # Some posters (HumanTier stub) may not be the AskStore-writing kind.
        # We trust the AskStore to be the source of truth for the link, but
        # we also update the record so reconcile_one can find it.
        updated = record.model_copy(deep=True)
        updated.link_ask(ask_id)
        self.eval_store.append(updated)


def _latest_records_per_input(records: list[EvalRecord]) -> list[EvalRecord]:
    latest: dict[str, EvalRecord] = {}
    for record in records:
        latest[record.input_id] = record
    return list(latest.values())


def _stable_dumps(value: Any) -> str:
    """Stable JSON-ish dump for set-membership comparison of tier outputs."""

    import json

    return json.dumps(value, sort_keys=True, default=str)
