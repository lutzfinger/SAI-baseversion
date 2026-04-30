"""Tests for GmailLabelReconciler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.eval.reconciler import ReconciliationOutcome
from app.eval.reconcilers import GmailLabelReconciler
from app.eval.record import EvalRecord, RealitySource


def _now() -> datetime:
    return datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC)


def _make_record(*, applied_decision: dict[str, Any]) -> EvalRecord:
    return EvalRecord(
        task_id="email_classification",
        input_id="thread-123",
        input={"subject": "hi"},
        active_decision=applied_decision,
        decided_at=_now(),
    )


def _label_extractor(decision: dict[str, Any]) -> set[str]:
    """Pull the labels we applied out of an active_decision dict."""

    return set(decision.get("applied_labels", []))


def test_human_relabel_produces_human_label_observation() -> None:
    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        thread_labels_fn=lambda _tid: {"L1/Friends", "INBOX"},
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.OBSERVED
    assert result.reality is not None
    assert result.reality.source == RealitySource.HUMAN_LABEL
    assert result.reality.label == {"labels": ["L1/Friends"]}


def test_user_kept_applied_labels_emits_human_action_passive() -> None:
    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        thread_labels_fn=lambda _tid: {"L1/Personal", "INBOX"},
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.OBSERVED
    assert result.reality is not None
    assert result.reality.source == RealitySource.HUMAN_ACTION


def test_no_taxonomy_labels_remains_still_pending() -> None:
    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        thread_labels_fn=lambda _tid: {"INBOX", "IMPORTANT"},
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.STILL_PENDING


def test_thread_not_found_remains_still_pending() -> None:
    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        thread_labels_fn=lambda _tid: None,
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.STILL_PENDING


def test_thread_labels_fn_raising_does_not_propagate() -> None:
    def _broken(_tid: str) -> set[str] | None:
        raise RuntimeError("Gmail API down")

    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        thread_labels_fn=_broken,
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.STILL_PENDING


def test_only_taxonomy_labels_count_for_relabel_detection() -> None:
    """L1/L2 labels are taxonomy; INBOX/IMPORTANT/etc are not."""

    record = _make_record(applied_decision={"applied_labels": ["L1/Personal"]})
    reconciler = GmailLabelReconciler(
        task_id="email_classification",
        # User added IMPORTANT but kept L1/Personal — that's not a re-label.
        thread_labels_fn=lambda _tid: {"L1/Personal", "IMPORTANT"},
        applied_label_extractor=_label_extractor,
    )
    result = reconciler.reconcile_one(record, now=_now())
    assert result.outcome == ReconciliationOutcome.OBSERVED
    assert result.reality is not None
    assert result.reality.source == RealitySource.HUMAN_ACTION  # passive, not relabel
