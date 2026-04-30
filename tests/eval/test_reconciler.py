"""Tests for RealityReconciliationRunner — the generic dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.eval.reconciler import (
    RealityReconciler,
    RealityReconciliationRunner,
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.eval.record import (
    EvalRecord,
    ObservedReality,
    Prediction,
    RealitySource,
    RealityStatus,
)
from app.eval.storage import EvalRecordStore


def _decided_at() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_pending_record(
    *,
    record_id: str,
    input_id: str = "msg-1",
    task_id: str = "email_classification",
    is_ground_truth: bool = False,
) -> EvalRecord:
    return EvalRecord(
        record_id=record_id,
        task_id=task_id,
        input_id=input_id,
        input={"subject": "hi"},
        active_decision={"label": "personal"},
        decided_at=_decided_at(),
        reality_observation_window_ends_at=_decided_at() + timedelta(days=7),
        is_ground_truth=is_ground_truth,
        tier_predictions={
            "rules": Prediction(
                tier_id="rules", output={}, confidence=0.0, abstained=True
            ),
        },
    )


class _ScriptedReconciler:
    """Returns the same outcome for any record. Useful for testing the runner."""

    def __init__(
        self,
        *,
        task_id: str = "email_classification",
        result: ReconciliationResult | None = None,
    ) -> None:
        self.task_id = task_id
        self._result = result or ReconciliationResult(
            outcome=ReconciliationOutcome.STILL_PENDING
        )
        self.calls = 0

    def reconcile_one(
        self, record: EvalRecord, *, now: datetime | None = None
    ) -> ReconciliationResult:
        self.calls += 1
        return self._result


@pytest.fixture
def store(tmp_path: Path) -> EvalRecordStore:
    return EvalRecordStore(root=tmp_path / "eval")


def test_reconciler_protocol_satisfied() -> None:
    rec = _ScriptedReconciler()
    assert isinstance(rec, RealityReconciler)


def test_runner_observed_writes_updated_record(store: EvalRecordStore) -> None:
    record = _make_pending_record(record_id="r-1")
    store.append(record)

    observed = ObservedReality(
        label={"label": "friends"},
        source=RealitySource.HUMAN_LABEL,
        observed_at=_decided_at() + timedelta(days=2),
    )
    reconciler = _ScriptedReconciler(
        result=ReconciliationResult(
            outcome=ReconciliationOutcome.OBSERVED, reality=observed
        )
    )
    runner = RealityReconciliationRunner(
        eval_store=store, clock=lambda: _decided_at() + timedelta(days=2)
    )
    counts = runner.run_for_task(reconciler=reconciler)
    assert counts[ReconciliationOutcome.OBSERVED] == 1

    records = store.read_all("email_classification")
    assert len(records) == 2  # original + updated
    latest = records[-1]
    assert latest.is_ground_truth is True
    assert latest.reality_status == RealityStatus.OBSERVED
    assert latest.reality is not None
    assert latest.reality.label == {"label": "friends"}


def test_runner_skips_already_ground_truth_records(store: EvalRecordStore) -> None:
    settled = _make_pending_record(record_id="r-1", is_ground_truth=True)
    store.append(settled)
    reconciler = _ScriptedReconciler()
    runner = RealityReconciliationRunner(
        eval_store=store, clock=lambda: _decided_at() + timedelta(days=2)
    )
    counts = runner.run_for_task(reconciler=reconciler)
    assert counts[ReconciliationOutcome.SKIP] == 1
    assert reconciler.calls == 0


def test_runner_marks_expired_when_past_window(store: EvalRecordStore) -> None:
    record = _make_pending_record(record_id="r-1")
    store.append(record)
    reconciler = _ScriptedReconciler()
    # Clock at +14 days: past the 7-day window.
    runner = RealityReconciliationRunner(
        eval_store=store, clock=lambda: _decided_at() + timedelta(days=14)
    )
    counts = runner.run_for_task(reconciler=reconciler)
    assert counts[ReconciliationOutcome.EXPIRED] == 1
    assert reconciler.calls == 0  # expired before reconciler called

    records = store.read_all("email_classification")
    assert records[-1].reality_status == RealityStatus.SKIPPED
    assert records[-1].metadata.get("skip_reason") == "reality_window_expired"


def test_runner_handles_still_pending_without_writing(store: EvalRecordStore) -> None:
    record = _make_pending_record(record_id="r-1")
    store.append(record)
    reconciler = _ScriptedReconciler(
        result=ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)
    )
    runner = RealityReconciliationRunner(
        eval_store=store, clock=lambda: _decided_at() + timedelta(days=2)
    )
    counts = runner.run_for_task(reconciler=reconciler)
    assert counts[ReconciliationOutcome.STILL_PENDING] == 1

    records = store.read_all("email_classification")
    # No new line written; only the original record remains.
    assert len(records) == 1


def test_runner_folds_jsonl_log_to_latest_per_input(store: EvalRecordStore) -> None:
    record_v1 = _make_pending_record(record_id="r-1", input_id="msg-1")
    store.append(record_v1)
    # Append a "second classification pass" that supersedes the first.
    record_v2 = _make_pending_record(record_id="r-2", input_id="msg-1")
    store.append(record_v2)

    reconciler = _ScriptedReconciler()
    runner = RealityReconciliationRunner(
        eval_store=store, clock=lambda: _decided_at() + timedelta(days=2)
    )
    runner.run_for_task(reconciler=reconciler)
    assert reconciler.calls == 1  # folded to one record per input_id
