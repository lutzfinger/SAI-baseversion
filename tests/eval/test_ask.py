"""Tests for app.eval.ask — Ask record + AskStore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.eval.ask import Ask, AskKind, AskStatus, AskStore


def _posted_at() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_ask(
    *,
    task_id: str = "email_classification",
    status: AskStatus = AskStatus.OPEN,
    ask_id: str | None = None,
) -> Ask:
    kwargs = {
        "task_id": task_id,
        "kind": AskKind.CLASSIFICATION,
        "status": status,
        "record_ids": ["rec-1"],
        "question_text": "Classify?",
        "posted_to_channel": "#example",
        "posted_to_thread_ts": "1700000000.000100",
        "posted_at": _posted_at(),
        "expires_at": _posted_at() + timedelta(days=3),
    }
    if ask_id is not None:
        kwargs["ask_id"] = ask_id
    return Ask(**kwargs)


@pytest.fixture
def store(tmp_path: Path) -> AskStore:
    return AskStore(root=tmp_path / "eval")


def test_ask_default_status_is_open(store: AskStore) -> None:
    ask = _make_ask()
    assert ask.status == AskStatus.OPEN


def test_ask_extra_fields_rejected() -> None:
    with pytest.raises(ValueError):
        Ask(  # type: ignore[call-arg]
            task_id="x",
            kind=AskKind.CLASSIFICATION,
            question_text="?",
            posted_to_channel="#example",
            posted_at=_posted_at(),
            unexpected="nope",
        )


def test_store_round_trip(store: AskStore) -> None:
    ask = _make_ask()
    store.append(ask)
    [restored] = store.read_all("email_classification")
    assert restored == ask


def test_store_partitions_by_task(store: AskStore) -> None:
    store.append(_make_ask(task_id="email_classification"))
    store.append(_make_ask(task_id="travel"))
    assert len(store.read_all("email_classification")) == 1
    assert len(store.read_all("travel")) == 1


def test_latest_state_keeps_most_recent_per_id(store: AskStore) -> None:
    """The store is append-only; the latest line wins on ask_id collision."""

    ask = _make_ask(ask_id="ask-1")
    store.append(ask)
    answered = ask.model_copy(
        update={
            "status": AskStatus.ANSWERED,
            "answered_at": _posted_at() + timedelta(hours=2),
            "answer": {"label": "personal"},
        }
    )
    store.append(answered)

    latest = store.latest_state("email_classification")
    assert latest["ask-1"].status == AskStatus.ANSWERED
    assert latest["ask-1"].answer == {"label": "personal"}


def test_open_asks_filters_to_open_after_fold(store: AskStore) -> None:
    ask1 = _make_ask(ask_id="ask-1")
    ask2 = _make_ask(ask_id="ask-2")
    store.append(ask1)
    store.append(ask2)
    # Resolve ask-1
    store.append(
        ask1.model_copy(
            update={"status": AskStatus.ANSWERED, "answered_at": _posted_at()}
        )
    )
    open_asks = store.open_asks("email_classification")
    assert {a.ask_id for a in open_asks} == {"ask-2"}


def test_store_read_all_for_unknown_task_returns_empty(store: AskStore) -> None:
    assert store.read_all("nonexistent") == []
