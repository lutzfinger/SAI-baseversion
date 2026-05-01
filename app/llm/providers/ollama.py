"""Ollama Provider — local LLM via Ollama's native HTTP API.

Conforms to `app.llm.provider.Provider`. Uses Ollama's `/api/generate` endpoint
with the **native JSON Schema** format parameter (Ollama ≥0.5).

## Why native schema, not prompt-appended schema

The previous shape was `format: "json"` plus a "your output must match this
schema" tail appended to the prompt. That's the documented pattern for
older Ollama, but for harder-to-prompt local models like `gpt-oss:20b` it
produces an empty body for ~all calls — the model can't reliably emit
prompt-instructed structured JSON in one shot.

Ollama 0.5+ accepts the JSON Schema **directly** as the `format` value.
The runtime constrains generation to only tokens that satisfy the schema;
the model never has to "decide" to emit JSON. Empty bodies vanish.

If running an older Ollama, the `format` param falls back to "json" — a
plain JSON-mode call without schema enforcement. Detected via Ollama's
`/api/version` once per Provider instance.

Cost is 0 by default (local). If you self-host on rented GPU and want ROI
accounting, override `provider_id="ollama"` rates in the cost table.

## HTTP client: httpx

Uses `httpx` instead of `urllib.request`. Per the standard-libs-first
principle: httpx provides connection pooling, sane timeouts (separate
connect / read / write / pool budgets), automatic retries, and better
error surfacing than urllib's catch-all `URLError`. Adds httpx to the
core dependency set; the trade-off is small because httpx is already
present (it's a transitive of openai / langchain).
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

import httpx

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import LLMProviderError, LLMRequest, LLMResponse, TokenUsage

DEFAULT_HOST = "http://127.0.0.1:11434"

# Ollama versions ≥ this support passing a JSON schema dict as `format`.
# Older versions only accept the literal string "json".
_NATIVE_SCHEMA_MIN_VERSION: tuple[int, int, int] = (0, 5, 0)


class OllamaProvider:
    """Provider backed by a local Ollama server."""

    provider_id = "ollama"

    def __init__(
        self,
        *,
        model: str,
        host: str = DEFAULT_HOST,
        timeout_seconds: int = 45,
        cost_table: CostTable | None = None,
        force_legacy_json_format: bool = False,
        retries: int = 2,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._cost_table = cost_table or get_default_cost_table()
        self._force_legacy_json_format = force_legacy_json_format
        # Lazy: set on first predict() so construction stays cheap and
        # the Ollama daemon doesn't need to be up at import time.
        self._supports_native_schema: bool | None = None
        # httpx transport with built-in retry. Connect failures and
        # transient I/O errors are retried `retries` times with
        # exponential backoff before httpx raises. Eliminates the
        # "single transient blip → cascade abstain" failure mode that
        # the previous urllib-based implementation had.
        self._transport = httpx.HTTPTransport(retries=retries)
        self._client = httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(
                connect=5.0,
                read=float(timeout_seconds),
                write=5.0,
                pool=5.0,
            ),
        )

    def predict(self, request: LLMRequest) -> LLMResponse:
        if self._supports_native_schema is None:
            self._supports_native_schema = (
                False
                if self._force_legacy_json_format
                else _detect_native_schema_support(self._client, self.host)
            )

        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens

        if self._supports_native_schema:
            # Native schema mode: prompt stays clean, runtime enforces shape.
            prompt = request.prompt
            format_value: Any = request.response_schema
        else:
            # Legacy mode: append schema as a hint and rely on JSON mode.
            prompt = _augment_prompt_with_schema(
                prompt=request.prompt,
                schema=request.response_schema,
                schema_name=request.response_schema_name,
            )
            format_value = "json"

        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": format_value,
            "options": options,
        }

        started = perf_counter()
        try:
            response = self._client.post(f"{self.host}/api/generate", json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"Ollama request failed: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc
        latency_ms = int((perf_counter() - started) * 1000)

        raw_text = str(payload.get("response", "") or "").strip()
        if not raw_text:
            raise LLMProviderError(
                "Ollama response had empty body",
                provider_id=self.provider_id,
                model=self.model,
            )
        try:
            output = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(
                f"Ollama returned non-JSON output: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc

        usage = TokenUsage(
            input_tokens=int(payload.get("prompt_eval_count") or 0),
            output_tokens=int(payload.get("eval_count") or 0),
        )
        model_used = str(payload.get("model") or self.model)
        cost = self._cost_table.cost_for(
            provider_id=self.provider_id, model=model_used, usage=usage
        )
        return LLMResponse(
            output=output,
            raw_text=raw_text,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
            model_used=model_used,
            provider_id=self.provider_id,
        )


def _augment_prompt_with_schema(
    *, prompt: str, schema: dict[str, Any], schema_name: str
) -> str:
    """Append a schema hint to the prompt — fallback for Ollama <0.5.

    Used only when the daemon doesn't support native JSON Schema. Newer
    Ollama enforces the schema at generation time, so this string mangling
    isn't needed.
    """

    schema_blob = json.dumps(schema, indent=2)
    return (
        f"{prompt}\n\n"
        f"Respond with one JSON object matching this {schema_name} schema. "
        f"Output only the JSON object, no surrounding text or markdown.\n\n"
        f"Schema:\n{schema_blob}\n"
    )


def _detect_native_schema_support(client: httpx.Client, host: str) -> bool:
    """Probe `/api/version`. Return True if Ollama ≥0.5.

    Failure to reach the daemon defaults to False (safer: legacy path
    still works against any Ollama). Daemon reachability errors will
    surface again on the actual generate call with full context.

    Uses a 3-second connect+read budget — version probes shouldn't
    take longer than that, and we don't want to delay first call
    significantly if the daemon is briefly slow.
    """

    try:
        response = client.get(
            f"{host.rstrip('/')}/api/version",
            timeout=httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return False
    version_str = str(payload.get("version") or "")
    return _version_at_least(version_str, _NATIVE_SCHEMA_MIN_VERSION)


def _version_at_least(version_str: str, minimum: tuple[int, int, int]) -> bool:
    """Compare an Ollama version string ('0.17.7') to a (major, minor, patch) tuple."""

    parts = version_str.strip().lstrip("v").split(".")
    try:
        triple = tuple(int(part.split("-")[0]) for part in parts[:3])
    except ValueError:
        return False
    while len(triple) < 3:
        triple = triple + (0,)
    return triple >= minimum
