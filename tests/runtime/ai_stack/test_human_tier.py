"""Tests for HumanTier."""

from __future__ import annotations

from typing import Any

from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import AskPoster, HumanTier


class _StubAskPoster:
    """In-memory AskPoster that records calls and hands back a synthetic id."""

    def __init__(self, *, ask_id: str = "ask-001") -> None:
        self.ask_id = ask_id
        self.calls: list[dict[str, Any]] = []

    def post_ask(
        self,
        *,
        task_id: str,
        input_data: dict[str, Any],
        prior_predictions: dict[str, Any] | None = None,
        question_text: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "task_id": task_id,
                "input_data": input_data,
                "prior_predictions": prior_predictions,
                "question_text": question_text,
            }
        )
        return self.ask_id


def test_human_tier_always_abstains_and_records_ask_id() -> None:
    poster = _StubAskPoster(ask_id="ask-42")
    assert isinstance(poster, AskPoster)
    tier = HumanTier(tier_id="human", ask_poster=poster, task_id="email_classification")
    pred = tier.predict({"subject": "hi"})
    assert pred.abstained is True
    assert pred.confidence == 0.0
    assert pred.metadata["ask_id"] == "ask-42"
    assert pred.metadata["awaiting_human"] is True
    assert "ask-42" in (pred.reasoning or "")


def test_human_tier_passes_input_and_question_to_poster() -> None:
    poster = _StubAskPoster()
    tier = HumanTier(
        tier_id="human",
        ask_poster=poster,
        task_id="travel",
        question_template="Should I rebook with exit row at +$25?",
    )
    tier.predict({"booking_id": "abc"})
    [call] = poster.calls
    assert call["task_id"] == "travel"
    assert call["input_data"] == {"booking_id": "abc"}
    assert call["question_text"] == "Should I rebook with exit row at +$25?"


def test_human_tier_kind() -> None:
    poster = _StubAskPoster()
    tier = HumanTier(tier_id="human", ask_poster=poster, task_id="x")
    assert tier.tier_kind == TierKind.HUMAN
