"""Tests for LocalLLMTier and CloudLLMTier (shared LLMTierBase logic)."""

from __future__ import annotations

from typing import Any

from app.llm.provider import (
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    Provider,
    TokenUsage,
)
from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers import CloudLLMTier, LocalLLMTier


class _StubProvider:
    """Minimal Provider conforming to the Protocol."""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        response: LLMResponse | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._response = response
        self._raise = raise_exc
        self.calls: list[LLMRequest] = []

    def predict(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _confident_response(model: str, provider_id: str) -> LLMResponse:
    return LLMResponse(
        output={"label": "personal", "confidence": 0.88},
        raw_text='{"label": "personal", "confidence": 0.88}',
        usage=TokenUsage(input_tokens=120, output_tokens=18),
        cost_usd=0.0006,
        latency_ms=240,
        model_used=model,
        provider_id=provider_id,
    )


def _low_confidence_response(model: str, provider_id: str) -> LLMResponse:
    return LLMResponse(
        output={"label": "personal", "confidence": 0.4},
        raw_text="...",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        cost_usd=0.0,
        latency_ms=10,
        model_used=model,
        provider_id=provider_id,
    )


def _no_confidence_response(model: str, provider_id: str) -> LLMResponse:
    return LLMResponse(
        output={"label": "newsletters"},
        raw_text='{"label": "newsletters"}',
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model_used=model,
        provider_id=provider_id,
    )


def _renderer(input_data: dict[str, Any]) -> str:
    return f"Classify: {input_data.get('subject', '')}"


def test_local_llm_tier_resolves_when_self_reported_confidence_above_threshold() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        response=_confident_response("gpt-oss:20b", "ollama"),
    )
    assert isinstance(provider, Provider)
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
        confidence_threshold=0.7,
    )
    pred = tier.predict({"subject": "hi"})
    assert pred.tier_id == "local_llm"
    assert pred.tier_kind == TierKind.LOCAL_LLM if hasattr(pred, "tier_kind") else True
    assert pred.abstained is False
    assert pred.confidence == 0.88
    assert pred.output == {"label": "personal", "confidence": 0.88}
    assert pred.cost_usd == 0.0006
    assert provider.calls[0].prompt == "Classify: hi"


def test_local_llm_tier_abstains_below_threshold() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        response=_low_confidence_response("gpt-oss:20b", "ollama"),
    )
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
        confidence_threshold=0.7,
    )
    pred = tier.predict({"subject": "x"})
    assert pred.abstained is True
    assert pred.confidence == 0.4


def test_local_llm_tier_defaults_confidence_to_one_when_field_absent() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        response=_no_confidence_response("gpt-oss:20b", "ollama"),
    )
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
    )
    pred = tier.predict({"subject": "x"})
    assert pred.confidence == 1.0
    assert pred.abstained is False


def test_local_llm_tier_abstains_on_provider_error() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        raise_exc=LLMProviderError("connection refused", provider_id="ollama", model="gpt-oss:20b"),
    )
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
    )
    pred = tier.predict({"subject": "x"})
    assert pred.abstained is True
    assert pred.confidence == 0.0
    assert "ollama" in (pred.reasoning or "")


def test_cloud_llm_tier_kind_is_cloud() -> None:
    provider = _StubProvider(
        provider_id="openai",
        model="gpt-4o",
        response=_confident_response("gpt-4o", "openai"),
    )
    tier = CloudLLMTier(
        tier_id="cloud_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
    )
    assert tier.tier_kind == TierKind.CLOUD_LLM
    pred = tier.predict({"subject": "test"})
    assert pred.metadata["provider_id"] == "openai"
    assert pred.metadata["model"] == "gpt-4o"


def test_llm_tier_clamps_invalid_confidence() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        response=LLMResponse(
            output={"label": "x", "confidence": 1.7},  # out of range
            raw_text="...",
            usage=TokenUsage(),
            model_used="gpt-oss:20b",
            provider_id="ollama",
        ),
    )
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
    )
    pred = tier.predict({})
    assert pred.confidence == 1.0  # clamped


def test_llm_tier_handles_non_numeric_confidence() -> None:
    provider = _StubProvider(
        provider_id="ollama",
        model="gpt-oss:20b",
        response=LLMResponse(
            output={"label": "x", "confidence": "high"},
            raw_text="...",
            usage=TokenUsage(),
            model_used="gpt-oss:20b",
            provider_id="ollama",
        ),
    )
    tier = LocalLLMTier(
        tier_id="local_llm",
        provider=provider,
        prompt_renderer=_renderer,
        response_schema={"type": "object"},
    )
    pred = tier.predict({})
    assert pred.confidence == 1.0  # falls back to default
