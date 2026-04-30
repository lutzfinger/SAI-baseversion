"""Tests for the Provider Protocol shape and the request/response models."""

from __future__ import annotations

import pytest

from app.llm.provider import (
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    Provider,
    TokenUsage,
)


class _FakeProvider:
    """Minimal Provider conforming to the Protocol — used by Tier tests later."""

    provider_id = "fake"
    model = "fake-model"

    def __init__(self, *, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[LLMRequest] = []

    def predict(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return self.response


def test_fake_provider_satisfies_protocol() -> None:
    response = LLMResponse(
        output={"label": "personal"},
        raw_text='{"label": "personal"}',
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model_used="fake-model",
        provider_id="fake",
    )
    fake = _FakeProvider(response=response)
    assert isinstance(fake, Provider)


def test_request_temperature_bounds() -> None:
    LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=0.0)
    LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=2.0)
    with pytest.raises(ValueError):
        LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=2.1)
    with pytest.raises(ValueError):
        LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=-0.1)


def test_token_usage_rejects_negative() -> None:
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=-1)
    with pytest.raises(ValueError):
        TokenUsage(output_tokens=-1)


def test_response_cost_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        LLMResponse(
            output={},
            raw_text="",
            usage=TokenUsage(),
            cost_usd=-0.01,
            model_used="x",
            provider_id="x",
        )


def test_provider_error_carries_provider_and_model() -> None:
    err = LLMProviderError("boom", provider_id="openai", model="gpt-4o")
    assert err.provider_id == "openai"
    assert err.model == "gpt-4o"
    assert "[openai/gpt-4o]" in str(err)
