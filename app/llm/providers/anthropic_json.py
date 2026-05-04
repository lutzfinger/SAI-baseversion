"""Thin JSON-returning wrapper around Anthropic's Messages API.

Used by callers (second-opinion gate, cornell-delay-triage classifier,
future agents) that just want `predict_json(prompt) -> dict` and
don't need the full `LLMRequest` / `LLMResponse` shape of the
generic Provider Protocol.

Per PRINCIPLES.md §13 the underlying call still goes through
`AnthropicMessagesProvider` for cost-table integration + response
normalization. This module is the small adapter layer.

Per #24b the model id is NOT hardcoded — callers construct via
`for_role(role)` which reads `config/llm_registry.yaml`.

Per #6 fail-closed: malformed Anthropic output, network timeout, or
schema mismatch all raise; the caller (e.g. SecondOpinionTier) catches
and converts to a verdict=escalate.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from app.llm.provider import LLMProviderError, LLMRequest
from app.llm.providers.anthropic_messages import AnthropicMessagesProvider
from app.llm.registry import get_model_for_role
from app.shared.runtime_env import load_runtime_env_best_effort


# Generic JSON-object response schema (model returns whatever JSON
# the prompt asked for; we don't enforce inner shape here).
_OPEN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


class AnthropicJsonProvider:
    """Wraps AnthropicMessagesProvider with a one-shot
    ``predict_json(prompt) -> dict`` method.

    Failure modes:
      * No API key in env → raises LLMProviderError at construction
      * Anthropic API error → raises LLMProviderError
      * Response not parseable as JSON → raises LLMProviderError
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        timeout_seconds: int = 30,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            # Best-effort: try to load runtime.env, then re-check env.
            try:
                load_runtime_env_best_effort()
                key = os.environ.get("ANTHROPIC_API_KEY", "")
            except Exception:
                key = ""
        if not key:
            raise LLMProviderError(
                "ANTHROPIC_API_KEY not set (env or runtime.env)",
                provider_id="anthropic", model=model,
            )
        self._inner = AnthropicMessagesProvider(
            model=model,
            api_key=key,
            timeout_seconds=timeout_seconds,
        )
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def predict_json(
        self,
        prompt: str,
        *,
        schema: Optional[dict[str, Any]] = None,
        schema_name: str = "JsonReply",
    ) -> dict[str, Any]:
        """Send `prompt`, get back a parsed JSON object.

        Per PRINCIPLES.md §6a (every input + output guarded — schema
        enforcement at every boundary): callers SHOULD pass an
        explicit `schema` whenever the response shape is known. The
        Anthropic API enforces the schema via tool-call structured
        output — values outside the allowed shape are rejected
        upstream of this method, so the dict you get back is
        guaranteed to match.

        Open-ended schema (passing `None`) is allowed but should be
        treated as a code smell: if you know what you expect, say
        so. The default exists only for callers genuinely doing
        free-form extraction.
        """

        request = LLMRequest(
            prompt=prompt,
            response_schema=schema or _OPEN_JSON_SCHEMA,
            response_schema_name=schema_name,
            max_output_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        response = self._inner.predict(request)
        # LLMResponse.output is typed dict[str, Any] — Pydantic
        # already rejects non-dict at the inner Provider layer.
        return response.output


def for_role(role: str, **overrides: Any) -> "AnthropicJsonProvider":
    """Build a JSON Provider for the given LLM registry role.

    Per #24b — callers say `for_role("safety_gate_high")`, never
    `AnthropicJsonProvider(model="claude-sonnet-…")`.
    """

    model = get_model_for_role(role)
    return AnthropicJsonProvider(model=model, **overrides)
