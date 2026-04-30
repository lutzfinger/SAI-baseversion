"""CloudLLMTier — expensive-tier LLM via a cloud Provider (OpenAI, Anthropic, ...).

Same shape as LocalLLMTier; differs only in `tier_kind=CLOUD_LLM` so the
cascade orders it last (before HUMAN). Cost matters here and flows through
from the Provider's CostTable lookup, so per-task ROI accounting is automatic.
"""

from __future__ import annotations

from app.runtime.ai_stack.tier import TierKind
from app.runtime.ai_stack.tiers.llm_base import LLMTierBase


class CloudLLMTier(LLMTierBase):
    """LLM tier backed by a cloud Provider."""

    tier_kind = TierKind.CLOUD_LLM
