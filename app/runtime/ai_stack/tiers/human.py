"""HumanTier — Slack-mediated human escalation.

The cascade can't synchronously wait for a human, so HumanTier:
  1. Posts an ask via the configured AskPoster (Slack in production)
  2. Returns a Prediction with `abstained=True` and the ask_id in metadata
  3. The runner updates the EvalRecord with `ask_id` and `reality_status=ASKED`
  4. When the human replies, a separate reconciler updates the record's `reality`

By definition HumanTier never resolves a request — it always abstains. Its
purpose is to ENROLL the case for human review and tag the EvalRecord so
asynchronous reconciliation can find it.

The AskPoster Protocol is intentionally tiny here; step 5 ships the concrete
SlackAskUI implementation. For tests, a stub AskPoster is enough.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.eval.record import Prediction
from app.runtime.ai_stack.tier import TierKind


@runtime_checkable
class AskPoster(Protocol):
    """Posts a Slack-style human ask. Returns an opaque ask_id."""

    def post_ask(
        self,
        *,
        task_id: str,
        input_data: dict[str, Any],
        prior_predictions: dict[str, Any] | None = None,
        question_text: str | None = None,
    ) -> str:
        """Post an ask and return its ask_id (used to link an EvalRecord)."""
        ...


class HumanTier:
    """Final fallback tier: enrolls the input for human review via Slack."""

    tier_kind = TierKind.HUMAN

    def __init__(
        self,
        *,
        tier_id: str,
        ask_poster: AskPoster,
        task_id: str,
        question_template: str = (
            "I'm unsure how to handle this — could you decide?"
        ),
        # HumanTier is by construction always-abstain; threshold isn't
        # consulted by the cascade for it, but kept for Protocol parity.
        confidence_threshold: float = 1.0,
    ) -> None:
        self.tier_id = tier_id
        self.ask_poster = ask_poster
        self.task_id = task_id
        self.question_template = question_template
        self.confidence_threshold = confidence_threshold

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        try:
            ask_id = self.ask_poster.post_ask(
                task_id=self.task_id,
                input_data=input_data,
                question_text=self.question_template,
            )
        except Exception as exc:
            # Posting the ask failed (Slack down, channel missing, no auth,
            # etc.). The cascade can't get human input here, but it
            # shouldn't crash the whole run — abstain with the error in
            # metadata. The runner falls back per escalation_policy.
            return Prediction(
                tier_id=self.tier_id,
                output={},
                confidence=0.0,
                abstained=True,
                reasoning=f"ask_poster failed: {type(exc).__name__}: {exc}"[:240],
                metadata={
                    "awaiting_human": False,
                    "ask_failed": True,
                    "error_type": type(exc).__name__,
                },
            )
        return Prediction(
            tier_id=self.tier_id,
            output={},
            confidence=0.0,
            abstained=True,
            reasoning=f"awaiting human via ask {ask_id}",
            metadata={"ask_id": ask_id, "awaiting_human": True},
        )
