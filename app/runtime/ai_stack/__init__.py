"""AI Stack — the cascade engine.

Each task has a list of Tier instances ordered cheapest → most expensive.
At runtime, the cascade calls them in order; the first tier whose Prediction
isn't abstained AND clears its confidence_threshold resolves the request.
The next-most-expensive tier runs only if the cheaper one gave up — the
cascade is sequential with early-stop, never parallel.

  Tier (Protocol)        — what every tier implements
  TierKind (enum)        — RULES | CLASSIFIER | LOCAL_LLM | CLOUD_LLM | HUMAN

  Concrete tiers (public, framework-pure):
    RulesTier           — wraps a deterministic callable
    ClassifierTier      — wraps a small ML predict_proba callable
    LocalLLMTier        — wraps a local Provider (Ollama, llama.cpp, ...)
    CloudLLMTier        — wraps a cloud Provider (OpenAI, Anthropic, ...)
    HumanTier           — Slack escalation; always abstains, enrolls for review

The cascade runner ships in step 4 (task.py + runner.py).
"""

from __future__ import annotations

from app.runtime.ai_stack.runner import CascadeAbstainedError, TieredTaskRunner
from app.runtime.ai_stack.task import (
    EscalationPolicy,
    GraduationExperimentConfig,
    GraduationThresholds,
    Task,
    TaskConfig,
)
from app.runtime.ai_stack.tier import (
    TIER_KIND_ORDER,
    Tier,
    TierKind,
    is_resolved,
)
from app.runtime.ai_stack.tiers.classifier import ClassifierTier, ClassifyFn
from app.runtime.ai_stack.tiers.cloud_llm import CloudLLMTier
from app.runtime.ai_stack.tiers.human import AskPoster, HumanTier
from app.runtime.ai_stack.tiers.llm_base import LLMTierBase, PromptRenderer
from app.runtime.ai_stack.tiers.local_llm import LocalLLMTier
from app.runtime.ai_stack.tiers.rules import RuleFn, RulesTier

__all__ = [
    "TIER_KIND_ORDER",
    "AskPoster",
    "CascadeAbstainedError",
    "ClassifierTier",
    "ClassifyFn",
    "CloudLLMTier",
    "EscalationPolicy",
    "GraduationExperimentConfig",
    "GraduationThresholds",
    "HumanTier",
    "LLMTierBase",
    "LocalLLMTier",
    "PromptRenderer",
    "RuleFn",
    "RulesTier",
    "Task",
    "TaskConfig",
    "Tier",
    "TierKind",
    "TieredTaskRunner",
    "is_resolved",
]
