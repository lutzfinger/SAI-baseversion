"""LocalLLMTier — cheap-tier LLM via a local Provider (Ollama, llama.cpp, ...).

Same shape as CloudLLMTier; differs only in `tier_kind=LOCAL_LLM`. The
underlying Provider does the actual work; the tier handles cascade-relevant
concerns (confidence threshold → abstain, cost passthrough).
"""

from __future__ import annotations

from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers.llm_base import LLMTierBase


class LocalLLMTier(LLMTierBase):
    """LLM tier backed by a local Provider."""

    tier_kind = TierKind.LOCAL_LLM
