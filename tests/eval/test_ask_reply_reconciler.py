"""Tests for AskReplyReconciler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.eval.ask import Ask, AskKind, AskStatus, AskStore
from app.eval.reconciler import ReconciliationOutcome
from app.eval.reconcilers import AskReplyReconciler
from app.eval.record import EvalRecord, RealitySource, RealityStatus
from app.eval.storage import EvalRecordStore


def _now() -> datetime:
    return datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC)


def _make_record(record_id: str = "r-1") -> EvalRecord:
    return EvalRecord(
        record_id=record_id,
        task_id="email_classification",
        input_id="msg-1",
        input={"subject": "hi"},
        active_decision={"label": "personal"},
        decided_at=_now() - timedelta(hours=2),
    )


def _make_open_ask(*, ask_id: str, record_ids: list[str], expires_in: timedelta) -> Ask:
    return Ask(
        ask_id=ask_id,
        task_id="email_classification",
        kind=AskKind.CLASSIFICATION,
        status=AskStatus.OPEN,
        record_ids=record_ids,
        question_text="?",
        posted_to_channel="#example",
        posted_to_thread_ts="1111111111.000100",
        posted_at=_now() - timedelta(hours=2),
        expires_at=_now() + expires_in,
    )


class _StubWebClient:
    def __init__(
        self,
        *,
        replies: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._replies = replies or []
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return {"messages": self._replies}


@pytest.fixture
def stores(tmp_path: Path) -> tuple[AskStore, EvalRecordStore]:
    return (
        AskStore(root=tmp_path / "eval"),
        EvalRecordStore(root=tmp_path / "eval"),
    )


def test_polling_marks_ask_answered_and_propagates_to_records(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    record = _make_record(record_id="r-1")
    eval_store.append(record)
    record.ask_id = "ask-1"
    eval_store.append(record)

    ask = _make_open_ask(ask_id="ask-1", record_ids=["r-1"], expires_in=timedelta(days=2))
    ask_store.append(ask)

    client = _StubWebClient(
        replies=[
            {"user": "BOT", "text": "[email_classification] needs input"},
            {"user": "U_LUTZ", "text": "friends", "ts": "1714492850.000100"},
        ]
    )
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        bot_user_id="BOT",
        clock=_now,
    )
    counts = reconciler.poll_open_asks()
    assert counts == {"answered": 1, "still_open": 0, "expired": 0}

    latest_state = ask_store.latest_state("email_classification")
    assert latest_state["ask-1"].status == AskStatus.ANSWERED
    assert latest_state["ask-1"].answer == {"text": "friends"}
    assert latest_state["ask-1"].answered_by == "U_LUTZ"

    # Linked record propagated to ground truth via SLACK_ASK
    records = eval_store.read_all("email_classification")
    latest = records[-1]
    assert latest.is_ground_truth is True
    assert latest.reality_status == RealityStatus.ANSWERED
    assert latest.reality is not None
    assert latest.reality.source == RealitySource.SLACK_ASK
    assert latest.reality.label == {"text": "friends"}


def test_polling_keeps_open_when_no_human_reply(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    ask_store.append(
        _make_open_ask(ask_id="ask-1", record_ids=[], expires_in=timedelta(days=2))
    )
    # Only the bot's own message in thread.
    client = _StubWebClient(
        replies=[
            {"user": "BOT", "text": "ask body"},
        ]
    )
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        bot_user_id="BOT",
        clock=_now,
    )
    counts = reconciler.poll_open_asks()
    assert counts == {"answered": 0, "still_open": 1, "expired": 0}
    assert ask_store.latest_state("email_classification")["ask-1"].status == AskStatus.OPEN


def test_polling_marks_expired_when_window_passed(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    ask_store.append(
        _make_open_ask(
            ask_id="ask-1", record_ids=[], expires_in=-timedelta(hours=1)
        )
    )
    client = _StubWebClient()
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        clock=_now,
    )
    counts = reconciler.poll_open_asks()
    assert counts == {"answered": 0, "still_open": 0, "expired": 1}
    assert client.calls == []  # didn't poll Slack for expired
    assert (
        ask_store.latest_state("email_classification")["ask-1"].status
        == AskStatus.EXPIRED
    )


def test_polling_tolerates_slack_errors(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    ask_store.append(
        _make_open_ask(ask_id="ask-1", record_ids=[], expires_in=timedelta(days=1))
    )
    client = _StubWebClient(raise_exc=RuntimeError("Slack 500"))
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        clock=_now,
    )
    counts = reconciler.poll_open_asks()
    # Stays open — we'll retry next poll.
    assert counts == {"answered": 0, "still_open": 1, "expired": 0}


def test_reconcile_one_returns_observed_when_ask_answered(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    record = _make_record(record_id="r-1")
    record.ask_id = "ask-1"
    eval_store.append(record)

    answered = _make_open_ask(
        ask_id="ask-1", record_ids=["r-1"], expires_in=timedelta(days=1)
    ).model_copy(
        update={
            "status": AskStatus.ANSWERED,
            "answered_at": _now(),
            "answered_by": "U_LUTZ",
            "answer": {"text": "friends"},
        }
    )
    ask_store.append(answered)

    client = _StubWebClient()
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        clock=_now,
    )
    result = reconciler.reconcile_one(record)
    assert result.outcome == ReconciliationOutcome.OBSERVED
    assert result.reality is not None
    assert result.reality.source == RealitySource.SLACK_ASK
    assert result.reality.label == {"text": "friends"}


def test_reconcile_one_still_pending_without_ask_id(
    stores: tuple[AskStore, EvalRecordStore],
) -> None:
    ask_store, eval_store = stores
    record = _make_record(record_id="r-1")  # ask_id is None
    client = _StubWebClient()
    reconciler = AskReplyReconciler(
        task_id="email_classification",
        client=client,
        ask_store=ask_store,
        eval_store=eval_store,
        clock=_now,
    )
    result = reconciler.reconcile_one(record)
    assert result.outcome == ReconciliationOutcome.STILL_PENDING
