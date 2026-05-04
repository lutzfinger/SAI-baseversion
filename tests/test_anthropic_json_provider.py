"""Tests for the AnthropicJsonProvider adapter (JSON-shaped wrapper).

Stubs out AnthropicMessagesProvider so no live API calls happen.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.providers import anthropic_json
from app.llm.provider import LLMProviderError, LLMResponse, TokenUsage


class _StubInner:
    def __init__(self, response_output: Any):
        self.response_output = response_output
        self.calls: list[Any] = []

    def predict(self, request):
        self.calls.append(request)
        if isinstance(self.response_output, Exception):
            raise self.response_output
        return LLMResponse(
            output=self.response_output,
            raw_text="(test)",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            cost_usd=0.001,
            latency_ms=10,
            model_used=request.response_schema_name,
            provider_id="anthropic",
        )


def _build(monkeypatch, inner_response):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    p = anthropic_json.AnthropicJsonProvider(model="claude-haiku-4-5-20251001")
    p._inner = _StubInner(inner_response)
    return p


def test_predict_json_returns_dict(monkeypatch):
    p = _build(monkeypatch, {"verdict": "allow", "reasoning": "ok"})
    out = p.predict_json("Decide allow or refuse.")
    assert out == {"verdict": "allow", "reasoning": "ok"}


# Note: non-dict outputs can't actually pass the LLMResponse Pydantic
# validator (output: dict[str, Any]) — Anthropic-side validation
# rejects them upstream of the adapter.


def test_predict_json_raises_on_inner_provider_error(monkeypatch):
    p = _build(monkeypatch, LLMProviderError(
        "boom", provider_id="anthropic", model="x",
    ))
    with pytest.raises(LLMProviderError, match="boom"):
        p.predict_json("anything")


def test_construction_without_api_key_fails(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Block the runtime.env loader from rescuing us.
    monkeypatch.setattr(
        anthropic_json, "load_runtime_env_best_effort", lambda: None,
    )
    with pytest.raises(LLMProviderError, match="not set"):
        anthropic_json.AnthropicJsonProvider(model="claude-haiku-4-5-20251001")


def test_for_role_uses_registry(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    p = anthropic_json.for_role("safety_gate_high")
    # Real registry → safety_gate_high is claude-sonnet per shipped config.
    assert p.model.startswith("claude-sonnet-")


def test_for_role_unknown_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(Exception):
        anthropic_json.for_role("nonexistent-role")
