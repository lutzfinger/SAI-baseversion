"""ClassifierTier — small ML classifier wrapped as a tier.

Designed for sklearn-style models that expose `predict_proba` plus an integer
class index → output mapping. The wrapper is callable-based so you can plug in
anything (logistic regression, random forest, BERT-tiny, etc.) — the tier
doesn't care about the model details, only that the callable returns
`(output_dict, confidence)`.

A real classifier ships in private overlays; public ships only the wrapper +
tests so any classifier flavor can be used as a Tier. Graduating a task from
LOCAL_LLM to CLASSIFIER is a common cost-reduction win once enough eval data
exists for supervised training.
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from app.eval.record import Prediction
from app.runtime.ai_stack.tier import TierKind

ClassifyFn = Callable[[dict[str, Any]], tuple[dict[str, Any], float]]


class ClassifierTier:
    """Wrap a `(input_dict) -> (output_dict, confidence)` callable as a tier.

    Differs from RulesTier in:
      - `tier_kind=CLASSIFIER` (escalation cost ordering)
      - higher default confidence threshold (classifiers tend to be smoother
        than rules; 0.85 is a reasonable starting point but tune from eval data)
      - any value below threshold counts as abstain (no separate sentinel needed)
    """

    tier_kind = TierKind.CLASSIFIER

    def __init__(
        self,
        *,
        tier_id: str,
        classify_fn: ClassifyFn,
        confidence_threshold: float = 0.85,
    ) -> None:
        self.tier_id = tier_id
        self.classify_fn = classify_fn
        self.confidence_threshold = confidence_threshold

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        started = perf_counter()
        try:
            output, confidence = self.classify_fn(input_data)
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            return Prediction(
                tier_id=self.tier_id,
                output={},
                confidence=0.0,
                abstained=True,
                latency_ms=latency_ms,
                reasoning=f"classifier error: {type(exc).__name__}",
            )
        latency_ms = int((perf_counter() - started) * 1000)
        if confidence < self.confidence_threshold:
            return Prediction(
                tier_id=self.tier_id,
                output=output,
                confidence=confidence,
                abstained=True,
                latency_ms=latency_ms,
                reasoning=f"classifier below threshold ({confidence:.2f})",
            )
        return Prediction(
            tier_id=self.tier_id,
            output=output,
            confidence=confidence,
            abstained=False,
            latency_ms=latency_ms,
            reasoning="classifier above threshold",
        )
