"""Provider protocol — vendor- and model-agnostic LLM API abstraction.

A `Provider` is bound to one specific (vendor, model). To switch models or
vendors, you instantiate a different Provider. Tiers (CloudLLMTier, LocalLLMTier)
hold a Provider instance and don't know about the underlying SDK.

Cost is the Provider's responsibility: its `predict()` returns the actual
USD cost of the call by consulting the cost table for its (vendor, model).

The Provider Protocol is small on purpose. Anything more specific (response
streaming, function calling, embeddings) goes on the concrete Provider class
and is invoked by Tiers that know they need it. The base Protocol covers
"give me a structured prediction for this prompt", which is what the cascade
needs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    """Token counts for one Provider call. Local providers may report 0s."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)  # OpenAI cached prompt discount


class LLMRequest(BaseModel):
    """One request to a Provider.

    `response_schema` is a JSON Schema dict (Draft 7 / 2020-12 mostly portable
    across vendors). Providers translate it to vendor-specific structured-output
    config (OpenAI: response_format=json_schema; Anthropic: tool with input_schema;
    Ollama: format=json + prompt-side schema instruction).
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str
    response_schema: dict[str, Any]
    response_schema_name: str = "Response"
    max_output_tokens: int | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """One response from a Provider — the structured output plus diagnostics."""

    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any]              # JSON-structured output, validated by Provider
    raw_text: str                       # original text body (for debug / fallback)
    usage: TokenUsage
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_ms: int = Field(default=0, ge=0)
    model_used: str                     # actual model that responded (vendor may downgrade)
    provider_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMProviderError(RuntimeError):
    """Raised when a Provider can't satisfy a request.

    Tier implementations catch this and treat it as `abstained=True` so the
    cascade can escalate. Distinguished from validation errors (caller bugs)
    by the `provider_id` and `model` attributes.
    """

    def __init__(self, message: str, *, provider_id: str, model: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.model = model

    def __str__(self) -> str:
        return f"[{self.provider_id}/{self.model}] {super().__str__()}"


@runtime_checkable
class Provider(Protocol):
    """Vendor-agnostic LLM Provider Protocol.

    Implementations must be bound to one specific (provider_id, model). The
    instance is stateless apart from any client connection it holds; calling
    `predict()` is safe to interleave from multiple Tiers.
    """

    provider_id: str                    # e.g., "openai", "anthropic", "google", "ollama"
    model: str                          # e.g., "gpt-4o", "claude-sonnet-4-5", "gpt-oss:20b"

    def predict(self, request: LLMRequest) -> LLMResponse:
        """Run one prediction. Raises LLMProviderError on transport / API errors."""
        ...
