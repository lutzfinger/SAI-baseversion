"""LLM Provider abstraction — vendor- and model-agnostic.

Tiers (CloudLLMTier, LocalLLMTier) hold a Provider instance and don't know
about the underlying SDK. Switching from one cloud vendor to another (or from
GPT-4o to gpt-5; from Ollama to llama.cpp) is a YAML edit, not a code change.

Providers shipped in public:
  - openai_responses : OpenAI Responses API (cloud)
  - ollama           : Local Ollama via HTTP (no extra deps)

Cost is computed per-call by the Provider via `app.llm.cost.CostTable`.
"""

from __future__ import annotations

from app.llm.cost import CostTable, get_default_cost_table
from app.llm.provider import (
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    Provider,
    TokenUsage,
)

__all__ = [
    "CostTable",
    "LLMProviderError",
    "LLMRequest",
    "LLMResponse",
    "Provider",
    "TokenUsage",
    "get_default_cost_table",
]
