"""Tests for app.eval.storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)
from app.eval.record import (
    EvalRecord,
    ObservedReality,
    Prediction,
    RealitySource,
)
from app.eval.storage import EvalRecordStore, PreferenceStore


def _decided_at() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_record(
    *,
    task_id: str = "email_classification",
    input_id: str = "msg-1",
) -> EvalRecord:
    return EvalRecord(
        task_id=task_id,
        input_id=input_id,
        input={"subject": "hi"},
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
        active_decision={"label": "personal"},
        decided_at=_decided_at(),
    )


def _approved_version() -> PreferenceVersion:
    return PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=_decided_at(),
        approved_at=_decided_at() + timedelta(hours=1),
        approved_by="lutz",
    )


@pytest.fixture
def record_store(tmp_path: Path) -> EvalRecordStore:
    return EvalRecordStore(root=tmp_path / "eval")


@pytest.fixture
def preference_store(tmp_path: Path) -> PreferenceStore:
    return PreferenceStore(root=tmp_path / "eval")


def test_record_store_round_trip(record_store: EvalRecordStore) -> None:
    record_store.append(_make_record(input_id="msg-1"))
    record_store.append(_make_record(input_id="msg-2"))
    records = record_store.read_all("email_classification")
    assert len(records) == 2
    assert {r.input_id for r in records} == {"msg-1", "msg-2"}


def test_record_store_returns_empty_for_unknown_task(
    record_store: EvalRecordStore,
) -> None:
    assert record_store.read_all("nonexistent_task") == []


def test_record_store_partitions_by_task(record_store: EvalRecordStore) -> None:
    record_store.append(_make_record(task_id="email_classification"))
    record_store.append(_make_record(task_id="travel"))
    assert len(record_store.read_all("email_classification")) == 1
    assert len(record_store.read_all("travel")) == 1


def test_record_store_preserves_observed_reality(
    record_store: EvalRecordStore,
) -> None:
    record = _make_record()
    record.record_reality(
        ObservedReality(
            label={"label": "friends"},
            source=RealitySource.HUMAN_LABEL,
            observed_at=_decided_at() + timedelta(days=2),
        )
    )
    record_store.append(record)
    [restored] = record_store.read_all("email_classification")
    assert restored.is_ground_truth is True
    assert restored.reality is not None
    assert restored.reality.source == RealitySource.HUMAN_LABEL
    assert restored.reality.label == {"label": "friends"}


def test_record_store_find_by_input_id(record_store: EvalRecordStore) -> None:
    record_store.append(_make_record(input_id="msg-1"))
    record_store.append(_make_record(input_id="msg-2"))
    record_store.append(_make_record(input_id="msg-1"))  # second pass
    matches = record_store.find_by_input_id("email_classification", "msg-1")
    assert len(matches) == 2


def test_preference_store_round_trip(preference_store: PreferenceStore) -> None:
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="Lutz prefers exit row",
        current=_approved_version(),
    )
    preference_store.upsert(pref)
    [restored] = preference_store.load("travel")
    assert restored.preference_id == pref.preference_id
    assert restored.current.rule_text == "prefer_exit_row"
    assert restored.is_active is True


def test_preference_store_upsert_replaces_by_id(
    preference_store: PreferenceStore,
) -> None:
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="initial",
        current=_approved_version(),
    )
    preference_store.upsert(pref)

    pref.description = "edited"
    preference_store.upsert(pref)

    [restored] = preference_store.load("travel")
    assert restored.description == "edited"


def test_preference_store_load_returns_empty_for_unknown_task(
    preference_store: PreferenceStore,
) -> None:
    assert preference_store.load("unknown") == []
