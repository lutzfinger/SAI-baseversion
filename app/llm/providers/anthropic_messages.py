"""Anthropic Messages API Provider with strict tool-based structured output.

Conforms to `app.llm.provider.Provider`. Anthropic doesn't have a json-schema
response_format like OpenAI; the canonical way to get strict structured output
is to declare a single tool whose input_schema is the response schema, and
force the model to use it via `tool_choice`.

Calls Anthropic's `/v1/messages` endpoint via stdlib `urllib.request` so we
don't take an `anthropic` SDK dependency. Cost is computed from the response
`usage` via `app.llm.cost.CostTable` against `provider_id="anthropic"`.
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import LLMProviderError, LLMRequest, LLMResponse, TokenUsage

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_API_VERSION = "2023-06-01"


class AnthropicMessagesProvider:
    """Provider backed by Anthropic's Messages API."""

    provider_id = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: int = 45,
        cost_table: CostTable | None = None,
    ) -> None:
        if not api_key:
            raise LLMProviderError(
                "api_key is required",
                provider_id=self.provider_id,
                model=model,
            )
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self._cost_table = cost_table or get_default_cost_table()

    def predict(self, request: LLMRequest) -> LLMResponse:
        # Tool with input_schema forces strict structured output via tool_choice.
        tool_name = request.response_schema_name
        tool = {
            "name": tool_name,
            "description": (
                f"Return one {tool_name} object matching the input_schema. "
                "Always invoke this tool; never reply with text."
            ),
            "input_schema": request.response_schema,
        }
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens or 1024,
            "messages": [
                {"role": "user", "content": request.prompt},
            ],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
            "temperature": request.temperature,
        }

        url = f"{self.base_url}/v1/messages"
        encoded = json.dumps(body).encode("utf-8")
        req = Request(  # noqa: S310 - safe URL construction
            url,
            data=encoded,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.api_version,
                "content-type": "application/json",
            },
            method="POST",
        )

        started = perf_counter()
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"Anthropic request failed: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc
        latency_ms = int((perf_counter() - started) * 1000)

        output, raw_text = _extract_tool_use(payload, tool_name=tool_name)
        if output is None:
            raise LLMProviderError(
                "Anthropic response did not contain a tool_use block",
                provider_id=self.provider_id,
                model=self.model,
            )

        usage = _extract_usage(payload)
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


def _extract_tool_use(
    payload: dict[str, Any], *, tool_name: str
) -> tuple[dict[str, Any] | None, str]:
    """Find the first tool_use block whose name matches the structured-output tool."""

    blocks = payload.get("content") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != tool_name:
            continue
        tool_input = block.get("input")
        if isinstance(tool_input, dict):
            return tool_input, json.dumps(tool_input)
    return None, ""


def _extract_usage(payload: dict[str, Any]) -> TokenUsage:
    raw = payload.get("usage") or {}

    def _read(name: str) -> int:
        value = raw.get(name)
        return int(value) if isinstance(value, int) else 0

    return TokenUsage(
        input_tokens=_read("input_tokens"),
        output_tokens=_read("output_tokens"),
        cached_input_tokens=_read("cache_read_input_tokens"),
    )
