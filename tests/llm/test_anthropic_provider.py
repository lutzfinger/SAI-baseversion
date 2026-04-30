"""Tests for app.llm.providers.anthropic_messages — monkeypatches urlopen."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.anthropic_messages import AnthropicMessagesProvider


def _zero_cost_table() -> CostTable:
    return CostTable(providers={"anthropic": {"*": {"input": 0.0, "output": 0.0}}})


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _payload_with_tool_use(
    tool_input: dict[str, Any], *, tool_name: str = "Response", model: str = "claude"
) -> dict[str, Any]:
    return {
        "id": "msg-001",
        "model": model,
        "content": [
            {"type": "text", "text": "I'll classify."},
            {"type": "tool_use", "id": "tu-1", "name": tool_name, "input": tool_input},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 25},
    }


def _opener(payload: dict[str, Any] | Exception):
    def _do(*_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        if isinstance(payload, Exception):
            raise payload
        return _FakeHttpResponse(payload)

    return _do


def test_predict_returns_tool_use_input_as_output() -> None:
    payload = _payload_with_tool_use({"label": "personal", "confidence": 0.9})
    provider = AnthropicMessagesProvider(
        model="claude-sonnet-4-5", api_key="k", cost_table=_zero_cost_table()
    )
    with patch("app.llm.providers.anthropic_messages.urlopen", side_effect=_opener(payload)):
        response = provider.predict(
            LLMRequest(
                prompt="Classify this email",
                response_schema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                },
            )
        )
    assert response.output == {"label": "personal", "confidence": 0.9}
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 25
    assert response.provider_id == "anthropic"


def test_predict_sends_tool_choice_to_force_structured_output() -> None:
    captured: dict[str, Any] = {}

    def _capture(req: Any, **_kwargs: Any) -> _FakeHttpResponse:
        captured["body"] = req.data.decode("utf-8")
        captured["headers"] = dict(req.headers)
        return _FakeHttpResponse(
            _payload_with_tool_use({"label": "x"}, tool_name="MyOutput")
        )

    provider = AnthropicMessagesProvider(
        model="claude-sonnet-4-5", api_key="my-key", cost_table=_zero_cost_table()
    )
    with patch("app.llm.providers.anthropic_messages.urlopen", side_effect=_capture):
        provider.predict(
            LLMRequest(
                prompt="Tag this",
                response_schema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                },
                response_schema_name="MyOutput",
                max_output_tokens=512,
            )
        )

    body = json.loads(captured["body"])
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 512
    assert body["tool_choice"] == {"type": "tool", "name": "MyOutput"}
    [tool] = body["tools"]
    assert tool["name"] == "MyOutput"
    assert tool["input_schema"]["properties"]["label"]["type"] == "string"
    # API key + version sent as headers, not in URL.
    assert captured["headers"].get("X-api-key") == "my-key"
    assert "Anthropic-version" in captured["headers"]


def test_predict_raises_when_no_tool_use_in_response() -> None:
    bad_payload = {
        "id": "x",
        "model": "claude",
        "content": [{"type": "text", "text": "I won't comply"}],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    provider = AnthropicMessagesProvider(
        model="claude-sonnet-4-5", api_key="k", cost_table=_zero_cost_table()
    )
    with (
        patch(
            "app.llm.providers.anthropic_messages.urlopen",
            side_effect=_opener(bad_payload),
        ),
        pytest.raises(LLMProviderError) as info,
    ):
        provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}))
    assert info.value.provider_id == "anthropic"


def test_predict_wraps_http_error_as_provider_error() -> None:
    provider = AnthropicMessagesProvider(
        model="claude-sonnet-4-5", api_key="k", cost_table=_zero_cost_table()
    )
    with (
        patch(
            "app.llm.providers.anthropic_messages.urlopen",
            side_effect=_opener(OSError("connection refused")),
        ),
        pytest.raises(LLMProviderError) as info,
    ):
        provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}))
    assert info.value.provider_id == "anthropic"


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(LLMProviderError):
        AnthropicMessagesProvider(model="claude-sonnet-4-5", api_key="")


def test_predict_extracts_cached_input_tokens() -> None:
    payload = {
        "id": "msg-001",
        "model": "claude-sonnet-4-5",
        "content": [
            {"type": "tool_use", "name": "Response", "input": {"label": "x"}}
        ],
        "usage": {
            "input_tokens": 200,
            "output_tokens": 10,
            "cache_read_input_tokens": 80,
        },
    }
    provider = AnthropicMessagesProvider(
        model="claude-sonnet-4-5", api_key="k", cost_table=_zero_cost_table()
    )
    with patch("app.llm.providers.anthropic_messages.urlopen", side_effect=_opener(payload)):
        response = provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )
    assert response.usage.cached_input_tokens == 80
