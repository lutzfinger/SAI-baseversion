"""Tier — the cascade's atomic unit.

Each Tier knows how to produce one Prediction for one task input. The cascade
runner (step 4) calls tiers in order from cheapest to active_tier; if a tier
returns `abstained=True` (or confidence below its `confidence_threshold`), the
cascade escalates to the next one.

Five concrete Tier kinds ship in public:

  RULES       — deterministic callables (regex, dict lookup, simple logic)
  CLASSIFIER  — small ML model wrapper (sklearn-style predict_proba)
  LOCAL_LLM   — Provider-backed local LLM (Ollama, llama.cpp, ...)
  CLOUD_LLM   — Provider-backed cloud LLM (OpenAI, Anthropic, Gemini, ...)
  HUMAN       — Slack escalation; returns abstain synchronously and tracks
                the ask for asynchronous reconciliation

Tier instances are stateless across calls (apart from any client connection
they hold). Multiple Task instances can share the same Tier instance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.eval.record import Prediction


class TierKind(StrEnum):
    """Recognized tier kinds, ordered cheapest → most expensive.

    SECOND_OPINION is a watchdog tier (per principles 16f and 10)
    inserted immediately before any side-effecting output. It does not
    produce a Prediction the way classifier tiers do — it produces
    a SecondOpinionVerdict over an upstream tier's proposed output.
    """

    RULES = "rules"
    CLASSIFIER = "classifier"
    LOCAL_LLM = "local_llm"
    CLOUD_LLM = "cloud_llm"
    SECOND_OPINION = "second_opinion"
    HUMAN = "human"


# Default cascade ordering. Tasks override per-instance via their tier list.
TIER_KIND_ORDER: tuple[TierKind, ...] = (
    TierKind.RULES,
    TierKind.CLASSIFIER,
    TierKind.LOCAL_LLM,
    TierKind.CLOUD_LLM,
    TierKind.SECOND_OPINION,
    TierKind.HUMAN,
)


@runtime_checkable
class Tier(Protocol):
    """One tier in the cascade.

    A Tier instance is bound to a specific task at construction time (it
    knows its prompt, schema, rule-fn, etc.). At runtime, the cascade just
    calls `predict(input_data)` on each tier in order until one returns a
    non-abstaining prediction whose `confidence >= confidence_threshold`.
    """

    tier_id: str                     # stable id, unique within a Task
    tier_kind: TierKind
    confidence_threshold: float      # below this OR abstained → escalate

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        """Run this tier on one task input. Always returns a Prediction.

        On error or "I don't know", set `abstained=True`. The cascade treats
        abstain identically to confidence below threshold, so a tier should
        prefer `abstained=True` over fabricating low-confidence output.
        """
        ...


def is_resolved(prediction: Prediction, *, threshold: float) -> bool:
    """Return True if the cascade should stop at this prediction."""

    if prediction.abstained:
        return False
    return prediction.confidence >= threshold
