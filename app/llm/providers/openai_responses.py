"""OpenAI Responses API Provider with strict structured output.

Conforms to `app.llm.provider.Provider`. Uses the Responses endpoint
(`client.responses.create`) which is OpenAI's preferred shape for structured
outputs and supersedes Chat Completions. The JSON Schema is normalized to
strict mode (every property listed under `required`, defaults stripped) before
being sent — this is what the OpenAI strict-output spec requires.

Cost is computed from `usage` via `app.llm.cost.CostTable` against
`provider_id="openai"` and the model name.

## Reasoning-model parameter handling

GPT-5.x and the o-series (o1, o3, o4) reject `temperature` outright with
`Unsupported parameter: 'temperature' is not supported with this model.`
The provider detects these by name prefix and silently drops `temperature`
from the payload. This is a vendor-specific quirk that belongs in the
Provider (per the pluggable Provider abstraction principle) — tier code
stays vendor-agnostic.

When the list of unsupported families changes, update
`_MODELS_WITHOUT_TEMPERATURE` below — tests cover the detection.
"""

from __future__ import annotations

import json
import re
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import LLMProviderError, LLMRequest, LLMResponse, TokenUsage

if TYPE_CHECKING:
    from openai import OpenAI

# Model families that reject `temperature` in the Responses API.
# Match prefixes against the canonical model id; case-insensitive.
# Keep this list narrow and explicit — silent parameter drops are easy to
# miss when debugging "why is my temperature being ignored?"
_MODELS_WITHOUT_TEMPERATURE: tuple[re.Pattern[str], ...] = (
    re.compile(r"^gpt-5(\.|-|$)", re.IGNORECASE),  # gpt-5, gpt-5-pro, gpt-5.2-pro, etc.
    re.compile(r"^o[1-9](-|$)", re.IGNORECASE),    # o1, o1-mini, o3, o3-mini, o4-mini, etc.
)


def _model_supports_temperature(model: str) -> bool:
    """Return True iff `temperature` may be sent for this model."""

    return not any(pattern.match(model) for pattern in _MODELS_WITHOUT_TEMPERATURE)


class OpenAIResponsesProvider:
    """Provider backed by the OpenAI Responses API."""

    provider_id = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 45,
        client: OpenAI | None = None,
        cost_table: CostTable | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise LLMProviderError(
                    "api_key is required when no client is provided",
                    provider_id=self.provider_id,
                    model=model,
                )
            try:
                from openai import OpenAI as OpenAIClient
            except ImportError as exc:  # pragma: no cover - openai is in deps
                raise LLMProviderError(
                    "openai package is not installed",
                    provider_id=self.provider_id,
                    model=model,
                ) from exc
            client_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout_seconds,
            }
            if base_url:
                client_kwargs["base_url"] = base_url
            client = OpenAIClient(**client_kwargs)
        self.model = model
        self.client = client
        self._cost_table = cost_table or get_default_cost_table()

    def predict(self, request: LLMRequest) -> LLMResponse:
        strict_schema = _to_strict_schema(request.response_schema)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": request.prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.response_schema_name,
                    "schema": strict_schema,
                    "strict": True,
                }
            },
        }
        if _model_supports_temperature(self.model):
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens

        started = perf_counter()
        try:
            response = self.client.responses.create(**payload)
        except Exception as exc:
            raise LLMProviderError(
                f"OpenAI request failed: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc
        latency_ms = int((perf_counter() - started) * 1000)

        raw_text = str(getattr(response, "output_text", "") or "").strip()
        if not raw_text:
            raise LLMProviderError(
                "OpenAI response had no output_text",
                provider_id=self.provider_id,
                model=self.model,
            )

        try:
            output = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(
                f"OpenAI returned non-JSON output: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc

        usage = _extract_usage(response)
        model_used = str(getattr(response, "model", self.model))
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


def _to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSON Schema for OpenAI strict structured outputs.

    OpenAI strict mode requires:
      - Every object property listed under `required`
      - No `default` keys
      - `additionalProperties: false` on every object
    """

    return _normalize_schema_node(schema)


def _normalize_schema_node(node: Any) -> Any:
    if isinstance(node, dict):
        normalized = {key: _normalize_schema_node(value) for key, value in node.items()}
        normalized.pop("default", None)
        if "properties" in normalized and isinstance(normalized["properties"], dict):
            properties = normalized["properties"]
            normalized["required"] = list(properties.keys())
            normalized.setdefault("additionalProperties", False)
        return normalized
    if isinstance(node, list):
        return [_normalize_schema_node(item) for item in node]
    return node


def _extract_usage(response: Any) -> TokenUsage:
    usage_obj = getattr(response, "usage", None)
    if usage_obj is None:
        return TokenUsage()

    def _read(name: str) -> int:
        value = getattr(usage_obj, name, None)
        return int(value) if isinstance(value, int) else 0

    cached = 0
    details = getattr(usage_obj, "input_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)

    return TokenUsage(
        input_tokens=_read("input_tokens"),
        output_tokens=_read("output_tokens"),
        cached_input_tokens=cached,
    )
