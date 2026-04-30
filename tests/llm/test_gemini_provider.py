"""Tests for app.llm.providers.gemini — monkeypatches urlopen."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.gemini import GeminiProvider, _to_gemini_schema


def _zero_cost_table() -> CostTable:
    return CostTable(providers={"google": {"*": {"input": 0.0, "output": 0.0}}})


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _payload_with_text(
    text: str, *, model: str = "gemini-2.5-pro"
) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": text}],
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 25,
        },
        "modelVersion": model,
    }


def _opener(payload: dict[str, Any] | Exception):
    def _do(*_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        if isinstance(payload, Exception):
            raise payload
        return _FakeHttpResponse(payload)

    return _do


def test_predict_parses_json_text_part() -> None:
    payload = _payload_with_text('{"label": "personal"}')
    provider = GeminiProvider(
        model="gemini-2.5-pro", api_key="k", cost_table=_zero_cost_table()
    )
    with patch("app.llm.providers.gemini.urlopen", side_effect=_opener(payload)):
        response = provider.predict(
            LLMRequest(
                prompt="Classify",
                response_schema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                },
            )
        )
    assert response.output == {"label": "personal"}
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 25
    assert response.provider_id == "google"


def test_predict_sends_response_schema_in_generation_config() -> None:
    captured: dict[str, Any] = {}

    def _capture(req: Any, **_kwargs: Any) -> _FakeHttpResponse:
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        return _FakeHttpResponse(_payload_with_text('{"x": 1}'))

    provider = GeminiProvider(
        model="gemini-2.5-pro", api_key="my-key", cost_table=_zero_cost_table()
    )
    with patch("app.llm.providers.gemini.urlopen", side_effect=_capture):
        provider.predict(
            LLMRequest(
                prompt="Answer",
                response_schema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                },
                max_output_tokens=512,
                temperature=0.3,
            )
        )

    # API key in query string per Gemini convention.
    assert "key=my-key" in captured["url"]
    body = json.loads(captured["body"])
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    schema = body["generationConfig"]["responseSchema"]
    assert schema["properties"]["x"]["type"] == "integer"
    # additionalProperties stripped (Gemini doesn't accept it).
    assert "additionalProperties" not in schema
    assert body["generationConfig"]["maxOutputTokens"] == 512
    assert body["generationConfig"]["temperature"] == 0.3


def test_predict_rejects_non_json_text() -> None:
    payload = _payload_with_text("not json")
    provider = GeminiProvider(
        model="gemini-2.5-pro", api_key="k", cost_table=_zero_cost_table()
    )
    with (
        patch("app.llm.providers.gemini.urlopen", side_effect=_opener(payload)),
        pytest.raises(LLMProviderError),
    ):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_predict_rejects_empty_candidates() -> None:
    provider = GeminiProvider(
        model="gemini-2.5-pro", api_key="k", cost_table=_zero_cost_table()
    )
    with (
        patch(
            "app.llm.providers.gemini.urlopen",
            side_effect=_opener({"candidates": []}),
        ),
        pytest.raises(LLMProviderError),
    ):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_predict_wraps_http_error_as_provider_error() -> None:
    provider = GeminiProvider(
        model="gemini-2.5-pro", api_key="k", cost_table=_zero_cost_table()
    )
    with (
        patch(
            "app.llm.providers.gemini.urlopen",
            side_effect=_opener(URLError("connection refused")),
        ),
        pytest.raises(LLMProviderError) as info,
    ):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )
    assert info.value.provider_id == "google"


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(LLMProviderError):
        GeminiProvider(model="gemini-2.5-pro", api_key="")


def test_to_gemini_schema_strips_unsupported_keys() -> None:
    schema = {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema",
        "properties": {
            "x": {"type": "integer", "default": 0},
            "nested": {
                "type": "object",
                "properties": {"y": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
    result = _to_gemini_schema(schema)
    assert "additionalProperties" not in result
    assert "$schema" not in result
    assert "default" not in result["properties"]["x"]
    assert "additionalProperties" not in result["properties"]["nested"]
    assert result["properties"]["nested"]["properties"]["y"]["type"] == "string"


# Keep URLError import local to its test.
from urllib.error import URLError  # noqa: E402
