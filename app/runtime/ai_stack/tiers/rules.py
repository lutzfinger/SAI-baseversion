"""RulesTier — deterministic callable wrapped as a tier.

Use for keyword matching, sender-domain lookup, regex-based routing — anything
that can decide confidently in microseconds without a model. The `rule_fn`
returns a tuple `(output, confidence)`; when no rule matches, return `(None, 0.0)`
or any pair where confidence is below the tier's threshold and the tier will
abstain.

This is the cheapest tier. Most tasks should aim to graduate as much load as
possible into rules, gated on eval-data-driven precision/recall thresholds.
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from app.eval.record import Prediction
from app.runtime.ai_stack.tier import TierKind

RuleFn = Callable[[dict[str, Any]], tuple[dict[str, Any] | None, float]]


class RulesTier:
    """Wrap a callable as a tier that conforms to the Tier Protocol."""

    tier_kind = TierKind.RULES

    def __init__(
        self,
        *,
        tier_id: str,
        rule_fn: RuleFn,
        confidence_threshold: float = 0.85,
        reasoning_template: str = "rules: {match}",
    ) -> None:
        self.tier_id = tier_id
        self.rule_fn = rule_fn
        self.confidence_threshold = confidence_threshold
        self._reasoning_template = reasoning_template

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        started = perf_counter()
        output, confidence = self.rule_fn(input_data)
        latency_ms = int((perf_counter() - started) * 1000)
        if output is None or confidence <= 0.0:
            return Prediction(
                tier_id=self.tier_id,
                output={},
                confidence=0.0,
                abstained=True,
                latency_ms=latency_ms,
                reasoning="rules: no match",
            )
        return Prediction(
            tier_id=self.tier_id,
            output=output,
            confidence=confidence,
            abstained=False,
            latency_ms=latency_ms,
            reasoning=self._reasoning_template.format(match=output),
        )
