"""Google Gemini Provider with native JSON Schema structured output.

Conforms to `app.llm.provider.Provider`. Calls Gemini's `generateContent`
endpoint via stdlib `urllib.request` (no `google-generativeai` SDK dependency).

Gemini supports structured output via `generationConfig.responseSchema` and
`responseMimeType="application/json"`. The schema dialect is mostly portable
JSON Schema, with a few field renames (e.g. `type` values must be uppercase
in some older versions); the conversion lives in `_to_gemini_schema`.
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import LLMProviderError, LLMRequest, LLMResponse, TokenUsage

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


# --- Response shape validation per principle 6a ---------------------------------
# Network response. `extra="ignore"` allows Gemini API growth without
# breaking us; we validate the fields we depend on are present + typed.

class _GeminiPart(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: Optional[str] = None


class _GeminiContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    parts: list[_GeminiPart] = Field(default_factory=list)


class _GeminiCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: Optional[_GeminiContent] = None


class _GeminiUsageMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    promptTokenCount: Optional[int] = 0
    candidatesTokenCount: Optional[int] = 0
    cachedContentTokenCount: Optional[int] = 0


class _GeminiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidates: list[_GeminiCandidate] = Field(default_factory=list)
    usageMetadata: Optional[_GeminiUsageMetadata] = None
    modelVersion: Optional[str] = None


class GeminiProvider:
    """Provider backed by Google's Gemini generateContent API."""

    provider_id = "google"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
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
        self.timeout_seconds = timeout_seconds
        self._cost_table = cost_table or get_default_cost_table()

    def predict(self, request: LLMRequest) -> LLMResponse:
        generation_config: dict[str, Any] = {
            "temperature": request.temperature,
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini_schema(request.response_schema),
        }
        if request.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_output_tokens

        body: dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": request.prompt}]},
            ],
            "generationConfig": generation_config,
        }
        query = urlencode({"key": self.api_key})
        url = f"{self.base_url}/models/{self.model}:generateContent?{query}"
        encoded = json.dumps(body).encode("utf-8")
        req = Request(  # noqa: S310 - safe URL construction
            url,
            data=encoded,
            headers={"content-type": "application/json"},
            method="POST",
        )

        started = perf_counter()
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw_payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"Gemini request failed: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc
        latency_ms = int((perf_counter() - started) * 1000)

        try:
            payload = _GeminiResponse.model_validate(raw_payload)
        except ValidationError as exc:
            raise LLMProviderError(
                f"Gemini response failed schema validation: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc

        raw_text = _extract_text(payload)
        if not raw_text:
            raise LLMProviderError(
                "Gemini response had no text candidate",
                provider_id=self.provider_id,
                model=self.model,
            )
        try:
            output = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(
                f"Gemini returned non-JSON output: {exc}",
                provider_id=self.provider_id,
                model=self.model,
            ) from exc

        usage = _extract_usage(payload)
        model_used = str(payload.modelVersion or self.model)
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


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate JSON Schema → Gemini's slightly-different responseSchema dialect.

    Gemini drops `additionalProperties`, `default`, and several other
    JSON-Schema-specific keys. Pass through everything else; recursion handles
    nested objects/arrays.
    """

    return _strip_unsupported_keys(schema)


_UNSUPPORTED_KEYS = frozenset({"default", "additionalProperties", "$schema", "$id"})


def _strip_unsupported_keys(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_unsupported_keys(value)
            for key, value in node.items()
            if key not in _UNSUPPORTED_KEYS
        }
    if isinstance(node, list):
        return [_strip_unsupported_keys(item) for item in node]
    return node


def _extract_text(payload: _GeminiResponse) -> str:
    """First candidate's first text part. Gemini may include several candidates;
    we take the first one — the model picks ranked order."""

    for candidate in payload.candidates:
        if candidate.content is None:
            continue
        for part in candidate.content.parts:
            if part.text:
                return part.text.strip()
    return ""


def _extract_usage(payload: _GeminiResponse) -> TokenUsage:
    raw = payload.usageMetadata
    if raw is None:
        return TokenUsage(input_tokens=0, output_tokens=0, cached_input_tokens=0)
    return TokenUsage(
        input_tokens=raw.promptTokenCount or 0,
        output_tokens=raw.candidatesTokenCount or 0,
        cached_input_tokens=raw.cachedContentTokenCount or 0,
    )
