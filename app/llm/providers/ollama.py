"""Ollama Provider — local LLM via Ollama's native HTTP API.

Conforms to `app.llm.provider.Provider`. Uses Ollama's `/api/generate` endpoint
in JSON mode (`format: "json"`). Ollama doesn't enforce JSON Schema, so the
schema is appended to the prompt as a "your output must match this schema"
hint and we validate the response on our side.

Cost is 0 by default (local). If you self-host on rented GPU and want ROI
accounting, override `provider_id="ollama"` rates in the cost table.

Uses `urllib.request` from the standard library to avoid pulling httpx into the
core dependency set. Connections are short-lived (one request per predict call).
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import LLMProviderError, LLMRequest, LLMResponse, TokenUsage

DEFAULT_HOST = "http://127.0.0.1:11434"


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
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._cost_table = cost_table or get_default_cost_table()

    def predict(self, request: LLMRequest) -> LLMResponse:
        prompt = _augment_prompt_with_schema(
            prompt=request.prompt,
            schema=request.response_schema,
            schema_name=request.response_schema_name,
        )
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens

        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": options,
        }
        url = f"{self.host}/api/generate"
        encoded = json.dumps(body).encode("utf-8")
        req = Request(  # noqa: S310 - safe URL construction
            url,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        started = perf_counter()
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
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
    """Append a schema hint to the prompt so the local model knows the shape.

    Ollama's `format: "json"` ensures the response is valid JSON but doesn't
    enforce schema. Adding a "schema spec" tail to the prompt is the
    industry-standard nudge for local models that don't have native structured
    output support.
    """

    schema_blob = json.dumps(schema, indent=2)
    return (
        f"{prompt}\n\n"
        f"Respond with one JSON object matching this {schema_name} schema. "
        f"Output only the JSON object, no surrounding text or markdown.\n\n"
        f"Schema:\n{schema_blob}\n"
    )
