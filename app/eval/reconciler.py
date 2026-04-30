"""RealityReconciler — closes the eval loop.

Reality is the ONLY source of ground truth. The cascade writes EvalRecords
with `reality_status=PENDING` and a 7-day window. A RealityReconciler scans
pending records and looks for reality observations:

  - The user re-tagged the email in Gmail (HUMAN_LABEL)
  - The user archived without responding (HUMAN_ACTION; may be ambiguous)
  - The user replied in a Slack ask thread (SLACK_ASK)
  - A booking confirmation appeared in calendar (BOOKING_CONFIRMATION)

Each task gets its own concrete reconciler: a `GmailLabelReconciler` for email
classification, a `CalendarReconciler` for travel, etc. They share this
protocol so the reconciliation runner is generic.

Public ships:
  - The `RealityReconciler` Protocol
  - `RealityReconciliationRunner` — generic scanner that walks pending records
    and dispatches to the configured reconciler(s)
  - `AskReplyReconciler` — closes the Ask loop by polling Slack threads for
    replies and marking Asks/Records accordingly
  - `GmailLabelReconciler` (next file) — task-specific reconciler scaffold

Per-task wiring lives in private overlays.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.eval.record import EvalRecord, ObservedReality
from app.eval.storage import EvalRecordStore


class ReconciliationOutcome(StrEnum):
    """What a reconciler found for one record."""

    OBSERVED = "observed"          # reality found; record updated to ground truth
    STILL_PENDING = "still_pending"  # nothing yet; check again later
    EXPIRED = "expired"            # window passed; mark skipped
    SKIP = "skip"                  # record is not eligible (already ground truth, etc.)


class ReconciliationResult:
    """Per-record outcome from one reconciler call."""

    __slots__ = ("outcome", "reality", "notes")

    def __init__(
        self,
        *,
        outcome: ReconciliationOutcome,
        reality: ObservedReality | None = None,
        notes: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.reality = reality
        self.notes = notes


@runtime_checkable
class RealityReconciler(Protocol):
    """Per-task reconciler. Each implementation knows its observation surface."""

    task_id: str

    def reconcile_one(
        self, record: EvalRecord, *, now: datetime | None = None
    ) -> ReconciliationResult:
        """Return what we observed for this record (or that we observed nothing)."""
        ...


class RealityReconciliationRunner:
    """Generic runner: scans pending records, dispatches to a reconciler.

    Each task has one (or more) registered reconciler(s). The runner reads all
    pending EvalRecords for the task, calls reconcile_one for each, and writes
    back the updated record (a new line in the JSONL log; the latest wins on
    fold). Records past the reality window get marked SKIPPED.

    Storage layout note: append-only JSONL means we re-append the updated
    record. EvalRecordStore.read_all returns ALL lines; a separate fold step
    by record_id collapses to the latest. (Phase 6 follow-up may add a fold
    helper if call sites need it; for now the reconciler just appends.)
    """

    def __init__(
        self,
        *,
        eval_store: EvalRecordStore,
        clock: Any = None,
    ) -> None:
        self.eval_store = eval_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_for_task(
        self, *, reconciler: RealityReconciler
    ) -> dict[ReconciliationOutcome, int]:
        """Reconcile every pending record for one task. Return outcome counts."""

        now = self._clock()
        counts: dict[ReconciliationOutcome, int] = {
            outcome: 0 for outcome in ReconciliationOutcome
        }
        latest = _latest_records_per_input(
            self.eval_store.read_all(reconciler.task_id)
        )
        for record in latest:
            if record.is_ground_truth:
                counts[ReconciliationOutcome.SKIP] += 1
                continue

            window_end = record.reality_observation_window_ends_at
            if window_end is not None and now > window_end and record.reality is None:
                # Window expired without reality. Mark as skipped (excluded
                # from training) and append.
                expired = record.model_copy(deep=True)
                expired.mark_skipped(reason="reality_window_expired")
                self.eval_store.append(expired)
                counts[ReconciliationOutcome.EXPIRED] += 1
                continue

            result = reconciler.reconcile_one(record, now=now)
            counts[result.outcome] += 1

            if result.outcome == ReconciliationOutcome.OBSERVED and result.reality:
                updated = record.model_copy(deep=True)
                updated.record_reality(result.reality)
                self.eval_store.append(updated)
        return counts


def _latest_records_per_input(records: list[EvalRecord]) -> list[EvalRecord]:
    """Fold the JSONL log: keep the last record line per (input_id) within task.

    EvalRecordStore is append-only, so reconciliation updates re-append; the
    last line for each input wins.
    """

    latest: dict[str, EvalRecord] = {}
    for record in records:
        latest[record.input_id] = record
    return list(latest.values())
