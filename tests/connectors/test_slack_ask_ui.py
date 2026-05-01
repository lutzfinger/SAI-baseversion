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
    # header + input + top_prediction + body
    assert len(blocks) == 4
    assert blocks[0]["type"] == "header"
    top_prediction_text = blocks[2]["text"]["text"]
    assert "Top prediction:" in top_prediction_text
    assert "cloud_llm" in top_prediction_text  # the highest-confidence non-abstain
    assert "0.62" in top_prediction_text
    body_text = blocks[3]["text"]["text"]
    assert "Right call?" in body_text
    assert "_Reply in this thread to answer._" in body_text


def test_post_ask_omits_top_prediction_block_when_no_predictions(
    store: AskStore,
) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="travel",
        input_data={"booking_id": "abc"},
    )
    [call] = client.calls
    assert len(call["blocks"]) == 3  # header + input + body (no top-prediction block)


def test_post_ask_email_input_renders_labeled_fields(store: AskStore) -> None:
    """Email-shaped inputs render with From / To / Subject / Summary fields,
    not as a JSON dump."""

    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)

    ui.post_ask(
        task_id="email_classification",
        input_data={
            "from_email": "alice@somecompany.example",
            "from_name": "Alice Example",
            "to": ["you@example.com"],
            "subject": "Following up on our customer call",
            "snippet": "Hi, just checking in on the next steps from yesterday's call.",
        },
        question_text="Which bucket?",
    )
    [call] = client.calls
    input_block = call["blocks"][1]["text"]["text"]
    assert "*Email:*" in input_block
    assert "*From:*" in input_block
    assert "Alice Example" in input_block
    assert "alice@somecompany.example" in input_block
    assert "*To:*" in input_block
    assert "you@example.com" in input_block
    assert "*Subject:*" in input_block
    assert "Following up on our customer call" in input_block
    assert "*Summary:*" in input_block
    assert "checking in on the next steps" in input_block
    # Definitely NOT the old JSON-dump format
    assert "```" not in input_block


def test_post_ask_truncates_email_summary_to_150_chars(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)
    long_body = "x " * 200  # 400 chars

    ui.post_ask(
        task_id="email_classification",
        input_data={
            "from_email": "alice@somecompany.example",
            "subject": "Long body",
            "body_excerpt": long_body,
        },
    )
    [call] = client.calls
    input_block = call["blocks"][1]["text"]["text"]
    summary_line = next(
        line for line in input_block.split("\n") if line.startswith("• *Summary:*")
    )
    body_part = summary_line.split("• *Summary:* ", 1)[1]
    assert len(body_part) <= 151  # 150 + "…"
    assert body_part.endswith("…")


def test_post_ask_non_email_input_falls_back_to_json(store: AskStore) -> None:
    client = _StubWebClient()
    ui = SlackAskUI(client=client, channel="#example", ask_store=store)
    ui.post_ask(
        task_id="travel",
        input_data={"booking_id": "abc", "destination": "MUC"},
    )
    [call] = client.calls
    input_block = call["blocks"][1]["text"]["text"]
    assert "*Input:*" in input_block
    assert "```" in input_block
    assert "booking_id" in input_block


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
