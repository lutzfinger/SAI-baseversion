"""Shared base for LocalLLMTier and CloudLLMTier.

Both LLM tiers do the same thing — render a prompt from input, call a Provider,
parse the response, wrap as a Prediction. They differ only in `tier_kind`,
which the cascade uses for ordering and reporting. Cost differences flow
naturally from the underlying Provider's CostTable lookup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.eval.record import Prediction
from app.llm.provider import LLMProviderError, LLMRequest, Provider
from app.runtime.ai_stack.tier import TierKind

PromptRenderer = Callable[[dict[str, Any]], str]


class LLMTierBase:
    """Internal base. Use LocalLLMTier or CloudLLMTier."""

    tier_kind: TierKind  # set by concrete subclass

    def __init__(
        self,
        *,
        tier_id: str,
        provider: Provider,
        prompt_renderer: PromptRenderer,
        response_schema: dict[str, Any],
        response_schema_name: str = "Response",
        confidence_threshold: float = 0.7,
        confidence_field: str = "confidence",
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.tier_id = tier_id
        self.provider = provider
        self.prompt_renderer = prompt_renderer
        self.response_schema = response_schema
        self.response_schema_name = response_schema_name
        self.confidence_threshold = confidence_threshold
        self.confidence_field = confidence_field
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature

    def predict(self, input_data: dict[str, Any]) -> Prediction:
        prompt = self.prompt_renderer(input_data)
        request = LLMRequest(
            prompt=prompt,
            response_schema=self.response_schema,
            response_schema_name=self.response_schema_name,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
        )
        try:
            response = self.provider.predict(request)
        except LLMProviderError as exc:
            return Prediction(
                tier_id=self.tier_id,
                output={},
                confidence=0.0,
                abstained=True,
                cost_usd=0.0,
                reasoning=f"provider error: {exc}",
            )

        confidence = _coerce_confidence(response.output, field=self.confidence_field)
        abstained = confidence < self.confidence_threshold
        return Prediction(
            tier_id=self.tier_id,
            output=response.output,
            confidence=confidence,
            abstained=abstained,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            reasoning=(
                f"{self.provider.provider_id}/{response.model_used}, "
                f"tokens={response.usage.input_tokens}+{response.usage.output_tokens}"
            ),
            metadata={
                "provider_id": response.provider_id,
                "model": response.model_used,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cached_input_tokens": response.usage.cached_input_tokens,
            },
        )


def _coerce_confidence(output: dict[str, Any], *, field: str) -> float:
    """Read confidence from the LLM output. Default to 1.0 if absent or invalid."""

    raw = output.get(field)
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, value))
