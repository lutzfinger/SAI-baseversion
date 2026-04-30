"""Tests for app.connectors.slack_ask_ui — SlackAskUI with mocked WebClient."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.connectors.slack_ask_ui import SlackAskUI
from app.eval.ask import AskKind, AskStatus, AskStore


class _StubWebClient:
    """Drop-in mock for slack_sdk.web.WebClient.chat_postMessage."""

    def __init__(self, *, ts: str = "1111111111.000100") -> None:
        self._ts = ts
        self.calls: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "ts": self._ts, "channel": kwargs.get("channel", "")}


@pytest.fixture
def store(tmp_path: Path) -> AskStore:
    return AskStore(root=tmp_path / "eval")


def test_post_ask_returns_id_and_persists_record(store: AskStore) -> None:
    client = _StubWebClient(ts="1111111111.000100")
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ask_id = ui.post_ask(
        task_id="email_classification",
        input_data={"subject": "hi", "from": "bob@example.com"},
        question_text="Tag this thread?",
    )
    assert ask_id

    asks = store.read_all("email_classification")
    assert len(asks) == 1
    persisted = asks[0]
    assert persisted.ask_id == ask_id
    assert persisted.status == AskStatus.OPEN
    assert persisted.kind == AskKind.CLASSIFICATION
    assert persisted.posted_to_channel == "#example"
    assert persisted.posted_to_thread_ts == "1111111111.000100"


def test_post_ask_calls_slack_with_blocks(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="email_classification",
        input_data={"subject": "hi"},
        prior_predictions={
            "rules": {"output": {}, "abstained": True, "confidence": 0.0},
            "cloud_llm": {
                "output": {"label": "personal"},
                "abstained": False,
                "confidence": 0.62,
            },
        },
        question_text="Right call?",
    )

    [call] = client.calls
    assert call["channel"] == "#example"
    assert "blocks" in call
    blocks = call["blocks"]
    # header + input + prior_predictions + body
    assert len(blocks) == 4
    assert blocks[0]["type"] == "header"
    body_text = blocks[3]["text"]["text"]
    assert "Right call?" in body_text
    assert "_Reply in this thread to answer._" in body_text


def test_post_ask_omits_prior_predictions_block_when_empty(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="travel",
        input_data={"booking_id": "abc"},
    )
    [call] = client.calls
    assert len(call["blocks"]) == 3  # header + input + body (no prior_predictions block)


def test_post_ask_includes_options_in_body(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="email_classification",
        input_data={"subject": "hi"},
        question_text="Which bucket?",
        options=["customers", "personal", "newsletters"],
    )
    [call] = client.calls
    body_text = call["blocks"][-1]["text"]["text"]
    assert "customers" in body_text
    assert "personal" in body_text
    assert "newsletters" in body_text

    [persisted] = store.read_all("email_classification")
    assert persisted.options == ["customers", "personal", "newsletters"]


def test_post_ask_persists_input_summary_in_metadata(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="email_classification",
        input_data={"subject": "hello", "from_email": "alice@example.com"},
    )
    [persisted] = store.read_all("email_classification")
    summary = persisted.metadata["input_summary"]
    assert "alice@example.com" in summary
    assert "hello" in summary


def test_post_ask_truncates_oversized_input_summary(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    # 5000-char body — well over the 800-char default cap.
    ui.post_ask(
        task_id="email_classification",
        input_data={"body": "x" * 5000},
    )
    [persisted] = store.read_all("email_classification")
    summary = persisted.metadata["input_summary"]
    assert len(summary) <= 800
