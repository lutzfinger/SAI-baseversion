"""Tests for app.llm.providers.ollama — monkeypatches urlopen.

Two modes covered:
  - native schema (Ollama ≥0.5): schema sent as the `format` value
  - legacy json mode (Ollama <0.5): schema appended to the prompt as a hint

Each test forces a mode via either `force_legacy_json_format=True` or by
mocking `/api/version` to report a version below the native-schema threshold.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.llm.cost import CostTable
from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.ollama import OllamaProvider, _version_at_least


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


def _fake_urlopen(generate_payload: dict[str, Any], version: str = "0.17.7"):
    """Return an opener that distinguishes /api/version from /api/generate calls."""

    def _opener(req_or_url: Any, **_kwargs: Any) -> _FakeHttpResponse:
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        if "/api/version" in url:
            return _FakeHttpResponse({"version": version})
        return _FakeHttpResponse(generate_payload)

    return _opener


# ─── native-schema mode (Ollama ≥0.5) ─────────────────────────────────────


def test_native_schema_predict_parses_output() -> None:
    payload = {
        "model": "gpt-oss:20b",
        "response": '{"label": "personal", "confidence": 0.7}',
        "prompt_eval_count": 120,
        "eval_count": 22,
    }
    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch(
        "app.llm.providers.ollama.urlopen",
        side_effect=_fake_urlopen(payload, version="0.17.7"),
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


def test_native_schema_format_is_schema_dict_not_string() -> None:
    """Body sent to Ollama 0.5+ must include the schema dict as `format`."""

    captured: dict[str, Any] = {}

    def _capturing_opener(req_or_url: Any, **_kwargs: Any) -> _FakeHttpResponse:
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        if "/api/version" in url:
            return _FakeHttpResponse({"version": "0.17.7"})
        captured["body"] = req_or_url.data.decode("utf-8")
        return _FakeHttpResponse(
            {
                "model": "gpt-oss:20b",
                "response": '{"x": 1}',
                "prompt_eval_count": 10,
                "eval_count": 2,
            }
        )

    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    provider = OllamaProvider(model="gpt-oss:20b", cost_table=_zero_cost_table())
    with patch("app.llm.providers.ollama.urlopen", side_effect=_capturing_opener):
        provider.predict(
            LLMRequest(
                prompt="Classify this",
                response_schema=schema,
                response_schema_name="MyOutput",
            )
        )
    body = json.loads(captured["body"])
    # In native mode the schema is the format value — runtime constrains
    # generation. The prompt is left clean (no appended Schema: block).
    assert body["format"] == schema
    assert body["model"] == "gpt-oss:20b"
    assert body["stream"] is False
    assert body["prompt"] == "Classify this"
    assert "Schema:" not in body["prompt"]


# ─── legacy json-mode (Ollama <0.5) ───────────────────────────────────────


def test_legacy_mode_appends_schema_to_prompt() -> None:
    """When the daemon is older than 0.5, the schema is hinted into the prompt."""

    captured: dict[str, Any] = {}

    def _capturing_opener(req_or_url: Any, **_kwargs: Any) -> _FakeHttpResponse:
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        if "/api/version" in url:
            return _FakeHttpResponse({"version": "0.4.9"})
        captured["body"] = req_or_url.data.decode("utf-8")
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


def test_force_legacy_json_format_skips_version_probe() -> None:
    """force_legacy_json_format=True bypasses /api/version detection."""

    captured: dict[str, Any] = {}

    def _capturing_opener(req_or_url: Any, **_kwargs: Any) -> _FakeHttpResponse:
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        # If we accidentally probe /api/version, fail loudly.
        assert "/api/version" not in url, "force_legacy must not call /api/version"
        captured["body"] = req_or_url.data.decode("utf-8")
        return _FakeHttpResponse(
            {"response": '{"x": 1}', "prompt_eval_count": 10, "eval_count": 2}
        )

    provider = OllamaProvider(
        model="gpt-oss:20b",
        cost_table=_zero_cost_table(),
        force_legacy_json_format=True,
    )
    with patch("app.llm.providers.ollama.urlopen", side_effect=_capturing_opener):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )
    body = json.loads(captured["body"])
    assert body["format"] == "json"


# ─── error paths ──────────────────────────────────────────────────────────


def test_predict_wraps_http_error_as_provider_error() -> None:
    def _broken(*_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        raise OSError("connection refused")

    provider = OllamaProvider(
        model="gpt-oss:20b",
        cost_table=_zero_cost_table(),
        force_legacy_json_format=True,
    )
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
    provider = OllamaProvider(
        model="gpt-oss:20b",
        cost_table=_zero_cost_table(),
        force_legacy_json_format=True,
    )
    with patch(
        "app.llm.providers.ollama.urlopen", side_effect=_fake_urlopen(payload)
    ), pytest.raises(LLMProviderError):
        provider.predict(
            LLMRequest(prompt="x", response_schema={"type": "object"})
        )


def test_max_output_tokens_passes_through_as_num_predict() -> None:
    captured: dict[str, Any] = {}

    def _capturing_opener(req_or_url: Any, **_kwargs: Any) -> _FakeHttpResponse:
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        if "/api/version" in url:
            return _FakeHttpResponse({"version": "0.17.7"})
        captured["body"] = req_or_url.data.decode("utf-8")
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
