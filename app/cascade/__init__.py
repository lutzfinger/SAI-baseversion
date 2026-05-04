"""Generic cascade runner for skill-protocol workflows.

Per PRINCIPLES.md §33a (skills compose, primitives are separate),
the cascade walking + audit log shape + short-circuit semantics
live in the framework. Skills declare their cascade in the manifest
and (for `rules`-tier custom logic) register handlers under their
``tier_id``.

Built-in handlers ship for the universal tier kinds:
  * cloud_llm — instantiates Provider via LLM registry role
  * second_opinion — wraps SecondOpinionTier with the gate's
    coercion semantics
  * human — short-circuits with a "stage proposal" outcome

Skills register handlers for their `rules`-tier custom logic via
``register_rules_handler(workflow_id, tier_id, fn)``.
"""

from app.cascade.runner import (
    CascadeContext,
    CascadeResult,
    CascadeStep,
    StepKind,
    register_rules_handler,
    run_cascade,
)

__all__ = [
    "CascadeContext",
    "CascadeResult",
    "CascadeStep",
    "StepKind",
    "register_rules_handler",
    "run_cascade",
]
