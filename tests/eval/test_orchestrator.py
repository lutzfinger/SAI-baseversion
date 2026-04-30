"""Tests for AskOrchestrator — coverage + budget + disagreement scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.eval.ask import Ask, AskKind, AskStatus, AskStore
from app.eval.orchestrator import AskOrchestrator
from app.eval.record import (
    EvalRecord,
    ObservedReality,
    Prediction,
    RealitySource,
    RealityStatus,
)
from app.eval.storage import EvalRecordStore


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_pending(
    *,
    record_id: str,
    bucket_label: str,
    tiers_agree: bool = False,
    decided_offset: timedelta = timedelta(hours=1),
) -> EvalRecord:
    if tiers_agree:
        predictions = {
            "rules": Prediction(
                tier_id="rules",
                output={"label": bucket_label},
                confidence=0.9,
            ),
            "cloud_llm": Prediction(
                tier_id="cloud_llm",
                output={"label": bucket_label},
                confidence=0.95,
            ),
        }
    else:
        predictions = {
            "rules": Prediction(
                tier_id="rules",
                output={"label": "newsletters"},
                confidence=0.6,
            ),
            "cloud_llm": Prediction(
                tier_id="cloud_llm",
                output={"label": bucket_label},
                confidence=0.62,
            ),
        }
    return EvalRecord(
        record_id=record_id,
        task_id="email_classification",
        input_id=record_id,
        input={"subject": "hi"},
        active_decision={"label": bucket_label},
        decided_at=_now() - decided_offset,
        reality_observation_window_ends_at=_now() + timedelta(days=6),
        tier_predictions=predictions,
    )


def _make_ground_truth(
    *,
    record_id: str,
    bucket_label: str,
) -> EvalRecord:
    record = _make_pending(record_id=record_id, bucket_label=bucket_label)
    record.record_reality(
        ObservedReality(
            label={"label": bucket_label},
            source=RealitySource.HUMAN_LABEL,
            observed_at=_now() - timedelta(hours=2),
        )
    )
    return record


def _bucket_from_record(record: EvalRecord) -> str | None:
    return record.active_decision.get("label")


class _StubPoster:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 0

    def post_ask(self, **kwargs: Any) -> str:
        self._next_id += 1
        ask_id = f"ask-{self._next_id:03d}"
        self.calls.append({"ask_id": ask_id, **kwargs})
        return ask_id


@pytest.fixture
def stores(tmp_path: Path) -> tuple[EvalRecordStore, AskStore]:
    return (
        EvalRecordStore(root=tmp_path / "eval"),
        AskStore(root=tmp_path / "eval"),
    )


def _orchestrator(
    eval_store: EvalRecordStore,
    ask_store: AskStore,
    *,
    poster: _StubPoster,
    daily_budget: int = 5,
    coverage_target: int = 100,
    min_priority_threshold: float = 0.10,
) -> AskOrchestrator:
    return AskOrchestrator(
        task_id="email_classification",
        ask_poster=poster,
        eval_store=eval_store,
        ask_store=ask_store,
        bucketing_fn=_bucket_from_record,
        daily_budget=daily_budget,
        coverage_target=coverage_target,
        min_priority_threshold=min_priority_threshold,
        clock=_now,
    )


def test_orchestrator_asks_disagreeing_undercovered_pending(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    eval_store, ask_store = stores
    eval_store.append(_make_pending(record_id="r-1", bucket_label="customers"))
    poster = _StubPoster()
    counts = _orchestrator(
        eval_store, ask_store, poster=poster, coverage_target=100
    ).review_pending()
    assert counts["asked"] == 1
    assert poster.calls[0]["task_id"] == "email_classification"

    # Record now has ask_id linked.
    [first, latest] = eval_store.read_all("email_classification")
    assert first.ask_id is None
    assert latest.ask_id == "ask-001"
    assert latest.reality_status == RealityStatus.ASKED


def test_orchestrator_skips_below_priority_threshold(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    """Tier agreement + bucket already over coverage target → priority below
    the threshold → skipped, not asked."""

    eval_store, ask_store = stores
    eval_store.append(
        _make_pending(record_id="r-1", bucket_label="customers", tiers_agree=True)
    )
    # Saturate coverage for `customers`
    for index in range(120):
        eval_store.append(
            _make_ground_truth(
                record_id=f"gt-{index}", bucket_label="customers"
            )
        )

    poster = _StubPoster()
    counts = _orchestrator(
        eval_store, ask_store, poster=poster, coverage_target=100,
        min_priority_threshold=0.20,
    ).review_pending()
    assert counts["asked"] == 0
    assert counts["skipped"] >= 1
    assert poster.calls == []

    # Record marked SKIPPED in latest fold
    records = eval_store.read_all("email_classification")
    latest_for_r1 = [r for r in records if r.record_id == "r-1"][-1]
    assert latest_for_r1.reality_status == RealityStatus.SKIPPED
    assert latest_for_r1.metadata.get("skip_reason") == "below_priority_threshold"


def test_orchestrator_respects_daily_budget(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    eval_store, ask_store = stores
    for index in range(8):
        eval_store.append(
            _make_pending(record_id=f"r-{index}", bucket_label="customers")
        )

    poster = _StubPoster()
    counts = _orchestrator(
        eval_store,
        ask_store,
        poster=poster,
        daily_budget=3,
        coverage_target=100,
    ).review_pending()
    assert counts["asked"] == 3
    assert counts["waited"] == 5
    assert len(poster.calls) == 3


def test_orchestrator_counts_existing_asks_against_budget(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    eval_store, ask_store = stores
    eval_store.append(_make_pending(record_id="r-1", bucket_label="customers"))

    # Record that 4 asks were already posted today.
    posted_at = _now() - timedelta(hours=4)
    for index in range(4):
        ask_store.append(
            Ask(
                ask_id=f"existing-{index}",
                task_id="email_classification",
                kind=AskKind.CLASSIFICATION,
                status=AskStatus.OPEN,
                question_text="?",
                posted_to_channel="#example",
                posted_at=posted_at,
            )
        )

    poster = _StubPoster()
    counts = _orchestrator(
        eval_store, ask_store, poster=poster, daily_budget=5
    ).review_pending()
    assert counts["asked"] == 1  # 5 - 4 already used


def test_orchestrator_skips_ground_truth_records(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    eval_store, ask_store = stores
    eval_store.append(
        _make_ground_truth(record_id="r-1", bucket_label="customers")
    )
    poster = _StubPoster()
    counts = _orchestrator(
        eval_store, ask_store, poster=poster
    ).review_pending()
    assert counts == {"asked": 0, "skipped": 0, "waited": 0, "below_threshold": 0}


def test_orchestrator_skips_records_already_asked(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    eval_store, ask_store = stores
    record = _make_pending(record_id="r-1", bucket_label="customers")
    record.link_ask("ask-existing")
    eval_store.append(record)

    poster = _StubPoster()
    counts = _orchestrator(
        eval_store, ask_store, poster=poster
    ).review_pending()
    assert counts["asked"] == 0


def test_orchestrator_prioritizes_high_disagreement_over_high_coverage(
    stores: tuple[EvalRecordStore, AskStore],
) -> None:
    """Two pending records: one in a saturated bucket with disagreement,
    one in an undercovered bucket where tiers agreed. Disagreement+coverage
    weighting picks one over the other based on the configured weights."""

    eval_store, ask_store = stores
    saturated_bucket_record = _make_pending(
        record_id="r-saturated", bucket_label="customers"
    )
    undercovered_agreement_record = _make_pending(
        record_id="r-undercovered",
        bucket_label="job_hunt",
        tiers_agree=True,
    )
    eval_store.append(saturated_bucket_record)
    eval_store.append(undercovered_agreement_record)
    for i in range(120):
        eval_store.append(
            _make_ground_truth(record_id=f"gt-c-{i}", bucket_label="customers")
        )

    poster = _StubPoster()
    # daily_budget=1 forces a strict priority decision.
    counts = _orchestrator(
        eval_store,
        ask_store,
        poster=poster,
        daily_budget=1,
        coverage_target=100,
    ).review_pending()
    assert counts["asked"] == 1
    # The saturated-bucket record has higher disagreement (0.5) but coverage
    # score 0; the undercovered record has 0 disagreement but coverage score
    # 1.0. With equal weights (0.5 each), undercovered wins (0.5 > 0.25).
    [call] = poster.calls
    assert call["input_data"] == undercovered_agreement_record.input
