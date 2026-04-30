"""Tests for app.eval.record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.eval.record import (
    EvalRecord,
    ObservedReality,
    Prediction,
    RealitySource,
    RealityStatus,
)


def _decided_at() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_record(**overrides) -> EvalRecord:
    defaults = dict(
        task_id="email_classification",
        input_id="msg-1",
        input={"subject": "hi"},
        active_decision={"label": "personal"},
        decided_at=_decided_at(),
    )
    defaults.update(overrides)
    return EvalRecord(**defaults)


def test_default_record_is_pending_and_not_ground_truth() -> None:
    record = _make_record()
    assert record.reality_status == RealityStatus.PENDING
    assert record.is_ground_truth is False
    assert record.reality is None
    assert record.escalation_chain == []


def test_record_reality_observed_sets_ground_truth() -> None:
    record = _make_record()
    reality = ObservedReality(
        label={"label": "friends"},
        source=RealitySource.HUMAN_LABEL,
        observed_at=_decided_at() + timedelta(days=2),
    )
    record.record_reality(reality)
    assert record.is_ground_truth is True
    assert record.reality_status == RealityStatus.OBSERVED
    assert record.reality is reality


def test_slack_ask_answer_sets_status_to_answered() -> None:
    record = _make_record()
    record.link_ask("ask-123")
    assert record.ask_id == "ask-123"
    assert record.reality_status == RealityStatus.ASKED

    answer = ObservedReality(
        label={"label": "friends"},
        source=RealitySource.SLACK_ASK,
        observed_at=_decided_at() + timedelta(hours=2),
    )
    record.record_reality(answer)
    assert record.reality_status == RealityStatus.ANSWERED
    assert record.is_ground_truth is True


def test_mark_skipped_excludes_from_ground_truth() -> None:
    record = _make_record()
    record.mark_skipped(reason="ambiguous_no_action")
    assert record.reality_status == RealityStatus.SKIPPED
    assert record.is_ground_truth is False
    assert record.metadata["skip_reason"] == "ambiguous_no_action"


def test_prediction_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError):
        Prediction(tier_id="rules", output={"x": 1}, confidence=1.5)
    with pytest.raises(ValueError):
        Prediction(tier_id="rules", output={"x": 1}, confidence=-0.1)


def test_prediction_abstained_is_falsy_by_default() -> None:
    pred = Prediction(tier_id="rules", output={"x": 1}, confidence=0.4)
    assert pred.abstained is False
    assert pred.cost_usd == 0.0
    assert pred.latency_ms == 0


def test_record_serialization_round_trip() -> None:
    record = _make_record(
        escalation_chain=["rules", "cloud_llm"],
        tier_predictions={
            "rules": Prediction(
                tier_id="rules", output={"x": 1}, confidence=0.4, abstained=True
            ),
            "cloud_llm": Prediction(
                tier_id="cloud_llm",
                output={"label": "personal"},
                confidence=0.91,
                cost_usd=0.0024,
                latency_ms=620,
            ),
        },
    )
    raw = record.model_dump_json()
    restored = EvalRecord.model_validate_json(raw)
    assert restored == record
    assert restored.tier_predictions["cloud_llm"].cost_usd == 0.0024


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValueError):
        EvalRecord(  # type: ignore[call-arg]
            task_id="email_classification",
            input_id="msg-1",
            input={"subject": "hi"},
            active_decision={"label": "personal"},
            decided_at=_decided_at(),
            unexpected_field="nope",
        )
