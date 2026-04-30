"""Tests for app.llm.providers.ollama — monkeypatches urlopen."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.ollama import OllamaProvider


def _zero_cost_table() -> CostTable:
    return CostTable(providers={"ollama": {"*": {"input": 0.0, "output": 0.0}}})


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _fake_urlopen(payload: dict[str, Any]):
    def _opener(*_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        return _FakeHttpResponse(payload)

    return _opener


def test_predict_returns_parsed_output() -> None:
    payload = {
        "model": "gpt-oss:20b",
        "response": '{"label": "personal", "confidence": 0.7}',
        "prompt_eval_count": 120,
        "eval_count": 22,
    }
    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch(
        "app.llm.providers.ollama.urlopen", side_effect=_fake_urlopen(payload)
    ):
        response = provider.predict(
            LLMRequest(
                prompt="Tag this email",
                response_schema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                },
            )
        )
    assert response.output == {"label": "personal", "confidence": 0.7}
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 22
    assert response.provider_id == "ollama"
    assert response.cost_usd == 0.0


def test_prompt_includes_schema_hint() -> None:
    captured: dict[str, Any] = {}

    def _capturing_opener(req: Any, **_kwargs: Any) -> _FakeHttpResponse:
        captured["body"] = req.data.decode("utf-8")
        return _FakeHttpResponse(
            {
                "model": "gpt-oss:20b",
                "response": '{"x": 1}',
                "prompt_eval_count": 10,
                "eval_count": 2,
            }
        )

    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch("app.llm.providers.ollama.urlopen", side_effect=_capturing_opener):
        provider.predict(
            LLMRequest(
                prompt="Classify this",
                response_schema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
                response_schema_name="MyOutput",
            )
        )
    body = json.loads(captured["body"])
    assert body["format"] == "json"
    assert body["model"] == "gpt-oss:20b"
    assert body["stream"] is False
    assert "MyOutput" in body["prompt"]
    assert "Schema:" in body["prompt"]


def test_predict_wraps_http_error_as_provider_error() -> None:
    def _broken(*_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        raise OSError("connection refused")

    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with (
        patch("app.llm.providers.ollama.urlopen", side_effect=_broken),
        pytest.raises(LLMProviderError) as info,
    ):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )
    assert info.value.provider_id == "ollama"


def test_predict_rejects_non_json_response_text() -> None:
    payload = {"model": "gpt-oss:20b", "response": "not json", "eval_count": 1}
    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch(
        "app.llm.providers.ollama.urlopen", side_effect=_fake_urlopen(payload)
    ), pytest.raises(LLMProviderError):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_max_output_tokens_passes_through_as_num_predict() -> None:
    captured: dict[str, Any] = {}

    def _capturing_opener(req: Any, **_kwargs: Any) -> _FakeHttpResponse:
        captured["body"] = req.data.decode("utf-8")
        return _FakeHttpResponse(
            {
                "response": '{"x": 1}',
                "prompt_eval_count": 10,
                "eval_count": 2,
            }
        )

    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch("app.llm.providers.ollama.urlopen", side_effect=_capturing_opener):
        provider.predict(
            LLMRequest(
                prompt="x",
                response_schema={"type": "object"},
                max_output_tokens=128,
                temperature=0.5,
            )
        )
    body = json.loads(captured["body"])
    assert body["options"]["num_predict"] == 128
    assert body["options"]["temperature"] == 0.5
