"""Tests for app.llm.providers.openai_responses — uses a fake OpenAI client."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.openai_responses import (
    OpenAIResponsesProvider,
    _model_supports_temperature,
    _to_strict_schema,
)


class _FakeResponses:
    def __init__(self, *, response: Any, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._response


class _FakeOpenAIClient:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses


def _zero_cost_table() -> CostTable:
    return CostTable(providers={})


def _build_response(
    *, output_text: str, model: str = "gpt-4o", usage: dict[str, int] | None = None
) -> Any:
    if usage is None:
        usage = {"input_tokens": 100, "output_tokens": 25, "cached_tokens": 0}
    usage_obj = SimpleNamespace(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        input_tokens_details=SimpleNamespace(cached_tokens=usage.get("cached_tokens", 0)),
    )
    return SimpleNamespace(output_text=output_text, model=model, usage=usage_obj)


def test_predict_returns_parsed_output_and_usage() -> None:
    fake = _FakeResponses(response=_build_response(output_text='{"label": "personal"}'))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    request = LLMRequest(
        prompt="Tag this email",
        response_schema={
            "type": "object",
            "properties": {"label": {"type": "string"}},
        },
        max_output_tokens=64,
    )
    response = provider.predict(request)
    assert response.output == {"label": "personal"}
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 25
    assert response.provider_id == "openai"
    assert response.model_used == "gpt-4o"


def test_predict_strict_schema_includes_required_and_no_additional_properties() -> None:
    fake = _FakeResponses(response=_build_response(output_text='{"label": "personal"}'))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    request = LLMRequest(
        prompt="x",
        response_schema={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "confidence": {"type": "number", "default": 0.5},
            },
        },
    )
    provider.predict(request)
    sent = fake.calls[0]
    sent_schema = sent["text"]["format"]["schema"]
    assert sent_schema["required"] == ["label", "confidence"]
    assert sent_schema["additionalProperties"] is False
    assert "default" not in sent_schema["properties"]["confidence"]


def test_predict_wraps_sdk_error_as_provider_error() -> None:
    fake = _FakeResponses(response=None, raise_exc=RuntimeError("connection refused"))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    with pytest.raises(LLMProviderError) as info:
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )
    assert info.value.provider_id == "openai"
    assert info.value.model == "gpt-4o"


def test_predict_rejects_non_json_output() -> None:
    fake = _FakeResponses(response=_build_response(output_text="not json"))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    with pytest.raises(LLMProviderError):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_predict_rejects_empty_output() -> None:
    fake = _FakeResponses(response=_build_response(output_text=""))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    with pytest.raises(LLMProviderError):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_predict_extracts_cached_input_tokens() -> None:
    fake = _FakeResponses(
        response=_build_response(
            output_text='{"x": 1}',
            usage={"input_tokens": 200, "output_tokens": 10, "cached_tokens": 80},
        )
    )
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    response = provider.predict(
        LLMRequest(prompt="x", response_schema={"type": "object"})
    )
    assert response.usage.cached_input_tokens == 80


def test_model_supports_temperature_legacy_models() -> None:
    """gpt-4o and pre-5 models DO accept temperature."""

    assert _model_supports_temperature("gpt-4o")
    assert _model_supports_temperature("gpt-4o-mini")
    assert _model_supports_temperature("gpt-4-turbo")
    assert _model_supports_temperature("gpt-3.5-turbo")


def test_model_supports_temperature_gpt5_family_rejected() -> None:
    """gpt-5 family rejects temperature — provider must drop it."""

    assert not _model_supports_temperature("gpt-5")
    assert not _model_supports_temperature("gpt-5-pro")
    assert not _model_supports_temperature("gpt-5.2-pro")
    assert not _model_supports_temperature("gpt-5-mini")


def test_model_supports_temperature_reasoning_models_rejected() -> None:
    """o1 / o3 / o4 reasoning models reject temperature."""

    assert not _model_supports_temperature("o1")
    assert not _model_supports_temperature("o1-mini")
    assert not _model_supports_temperature("o3")
    assert not _model_supports_temperature("o3-mini")
    assert not _model_supports_temperature("o4-mini")


def test_predict_drops_temperature_for_gpt5() -> None:
    """Payload sent to gpt-5.2-pro must NOT contain temperature."""

    fake = _FakeResponses(response=_build_response(output_text='{"x": 1}', model="gpt-5.2-pro"))
    provider = OpenAIResponsesProvider(
        model="gpt-5.2-pro", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=0.7))
    sent = fake.calls[0]
    assert "temperature" not in sent, f"temperature must be dropped for gpt-5.x; got {sent}"


def test_predict_includes_temperature_for_gpt4() -> None:
    """gpt-4o still receives temperature in the payload."""

    fake = _FakeResponses(response=_build_response(output_text='{"x": 1}'))
    provider = OpenAIResponsesProvider(
        model="gpt-4o", client=_FakeOpenAIClient(fake), cost_table=_zero_cost_table()
    )
    provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}, temperature=0.7))
    sent = fake.calls[0]
    assert sent.get("temperature") == 0.7


def test_to_strict_schema_handles_nested_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner": {"type": "string", "default": "x"},
                },
            },
        },
    }
    strict = _to_strict_schema(schema)
    assert strict["required"] == ["outer"]
    inner = strict["properties"]["outer"]
    assert inner["required"] == ["inner"]
    assert "default" not in inner["properties"]["inner"]
    assert inner["additionalProperties"] is False
