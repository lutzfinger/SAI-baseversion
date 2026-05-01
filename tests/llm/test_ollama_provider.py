"""Tests for app.llm.providers.ollama — uses httpx.MockTransport.

Two modes covered:
  - native schema (Ollama ≥0.5): schema sent as the `format` value
  - legacy json mode (Ollama <0.5): schema appended to the prompt as a hint

Each test forces a mode via either `force_legacy_json_format=True` or by
mocking `/api/version` to report a version below the native-schema threshold.

Uses httpx.MockTransport per the standard-libs-first principle: httpx's
own test fixtures are more robust than monkey-patching urllib.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.ollama import OllamaProvider, _version_at_least


def _zero_cost_table() -> CostTable:
    return CostTable(providers={"ollama": {"*": {"input": 0.0, "output": 0.0}}})


def _build_provider(
    *,
    handler,
    force_legacy: bool = False,
    model: str = "qwen2.5:7b",
) -> OllamaProvider:
    """Build a provider with a custom request-handler that returns
    canned httpx responses. Bypasses the network entirely.
    """

    provider = OllamaProvider(
        model=model,
        cost_table=_zero_cost_table(),
        force_legacy_json_format=force_legacy,
        retries=0,  # don't retry in tests
    )
    # Replace the client with one that uses our mock transport.
    transport = httpx.MockTransport(handler)
    provider._client = httpx.Client(transport=transport, timeout=30.0)
    return provider


def _generate_response(
    *,
    response_text: str = '{"x": 1}',
    model: str = "qwen2.5:7b",
    prompt_eval: int = 10,
    eval_count: int = 2,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "response": response_text,
            "prompt_eval_count": prompt_eval,
            "eval_count": eval_count,
        },
    )


def _version_response(version: str = "0.17.7") -> httpx.Response:
    return httpx.Response(200, json={"version": version})


# ─── native-schema mode (Ollama ≥0.5) ─────────────────────────────────────


def test_native_schema_predict_parses_output() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _version_response()
        captured["body"] = json.loads(request.content)
        return _generate_response(
            response_text='{"label": "personal", "confidence": 0.7}',
            prompt_eval=120, eval_count=22,
        )

    provider = _build_provider(handler=handler)
    response = provider.predict(LLMRequest(
        prompt="Tag this email",
        response_schema={"type": "object", "properties": {"label": {"type": "string"}}},
    ))
    assert response.output == {"label": "personal", "confidence": 0.7}
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 22


def test_native_schema_format_is_schema_dict_not_string() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _version_response("0.17.7")
        captured["body"] = json.loads(request.content)
        return _generate_response()

    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    provider = _build_provider(handler=handler)
    provider.predict(LLMRequest(
        prompt="Classify this",
        response_schema=schema,
        response_schema_name="MyOutput",
    ))
    body = captured["body"]
    assert body["format"] == schema  # native: dict, not "json" string
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["prompt"] == "Classify this"
    assert "Schema:" not in body["prompt"]


# ─── legacy json-mode (Ollama <0.5) ───────────────────────────────────────


def test_legacy_mode_appends_schema_to_prompt() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _version_response("0.4.9")
        captured["body"] = json.loads(request.content)
        return _generate_response()

    provider = _build_provider(handler=handler)
    provider.predict(LLMRequest(
        prompt="Classify this",
        response_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        response_schema_name="MyOutput",
    ))
    body = captured["body"]
    assert body["format"] == "json"
    assert "MyOutput" in body["prompt"]
    assert "Schema:" in body["prompt"]


def test_force_legacy_json_format_skips_version_probe() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return _generate_response()

    provider = _build_provider(handler=handler, force_legacy=True)
    provider.predict(LLMRequest(
        prompt="x", response_schema={"type": "object"},
    ))
    assert "/api/version" not in seen_paths
    assert "/api/generate" in seen_paths


# ─── error paths ──────────────────────────────────────────────────────────


def test_predict_wraps_http_error_as_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Force a connection-style error via httpx.ConnectError.
        raise httpx.ConnectError("connection refused")

    provider = _build_provider(handler=handler, force_legacy=True)
    with pytest.raises(LLMProviderError) as info:
        provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}))
    assert info.value.provider_id == "ollama"


def test_predict_rejects_non_json_response_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "x", "response": "not json", "eval_count": 1},
        )

    provider = _build_provider(handler=handler, force_legacy=True)
    with pytest.raises(LLMProviderError):
        provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}))


def test_predict_wraps_http_status_error() -> None:
    """5xx from Ollama → ProviderError, not a generic crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream busy")

    provider = _build_provider(handler=handler, force_legacy=True)
    with pytest.raises(LLMProviderError):
        provider.predict(LLMRequest(prompt="x", response_schema={"type": "object"}))


def test_max_output_tokens_passes_through_as_num_predict() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _version_response()
        captured["body"] = json.loads(request.content)
        return _generate_response()

    provider = _build_provider(handler=handler)
    provider.predict(LLMRequest(
        prompt="x",
        response_schema={"type": "object"},
        max_output_tokens=128,
        temperature=0.5,
    ))
    body = captured["body"]
    assert body["options"]["num_predict"] == 128
    assert body["options"]["temperature"] == 0.5


# ─── version comparison helper ────────────────────────────────────────────


def test_version_at_least_basic() -> None:
    assert _version_at_least("0.5.0", (0, 5, 0))
    assert _version_at_least("0.17.7", (0, 5, 0))
    assert _version_at_least("1.0.0", (0, 5, 0))


def test_version_at_least_below_threshold() -> None:
    assert not _version_at_least("0.4.9", (0, 5, 0))
    assert not _version_at_least("0.0.1", (0, 5, 0))


def test_version_at_least_handles_v_prefix_and_pre() -> None:
    assert _version_at_least("v0.17.7", (0, 5, 0))
    assert _version_at_least("0.17.7-rc1", (0, 5, 0))


def test_version_at_least_garbage_returns_false() -> None:
    assert not _version_at_least("not-a-version", (0, 5, 0))
    assert not _version_at_least("", (0, 5, 0))
