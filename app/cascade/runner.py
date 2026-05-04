"""Generic cascade runner (PRINCIPLES.md §33a).

A skill's runner.py becomes thin: load the manifest, register any
skill-specific rules handlers, call ``run_cascade(...)``. The
framework owns the walk, the audit log, the short-circuit
semantics, and the proposal-staging contract.

Tier kind dispatch:

    rules           → registry of skill-registered handlers
                     (``register_rules_handler``); falls through if
                     unregistered (no built-in default).
    cloud_llm       → built-in: instantiate Provider via LLM
                     registry role from tier.config['llm_role'].
                     Skill provides an `llm_handler_fn` if it needs
                     custom prompt construction (most skills do).
    second_opinion  → built-in: wraps SecondOpinionTier; reads
                     skill-provided gate Provider from context.
    human           → built-in: stages YAML proposal under
                     ``eval/proposed/<workflow_id>/<thread_id>.yaml``
                     and returns ``ready_to_propose``.

Anything else → escalate (fail closed per #6).

Verdict shape:

    no_op             — workflow short-circuited (e.g. bad sender)
    continue          — tier passed; cascade continues
    escalate          — route to next tier (typically human)
    ready_to_propose  — full path passed; proposal staged for ✅
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import yaml

LOGGER = logging.getLogger(__name__)


StepKind = Literal["no_op", "continue", "escalate", "ready_to_propose"]


@dataclass
class CascadeStep:
    """One verdict from one tier — the cascade builds a list of these."""

    kind: StepKind
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CascadeContext:
    """Bundle the runner threads through every tier handler.

    `inputs` — caller-supplied per-invocation data (the email being
    triaged, the operator request, etc.).
    `accumulated` — running dict that handlers append to. Subsequent
    handlers read from it.
    `extra` — skill-specific dependency injection (LLM client stubs,
    safety_gate Provider, etc.). Tests pass stubs; production wiring
    pulls from registry.
    """

    workflow_id: str
    inputs: dict[str, Any]
    accumulated: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CascadeResult:
    workflow_id: str
    final_verdict: StepKind
    final_reason: str
    proposal_path: Optional[str]
    audit_log: list[dict[str, Any]]
    accumulated: dict[str, Any]


# Skill-registered handlers for `rules`-tier custom logic.
# Key: (workflow_id, tier_id) → handler(ctx, tier_config) -> CascadeStep.
_RULES_HANDLERS: dict[tuple[str, str], Callable[[CascadeContext, dict[str, Any]], CascadeStep]] = {}


def register_rules_handler(
    workflow_id: str,
    tier_id: str,
    handler: Callable[[CascadeContext, dict[str, Any]], CascadeStep],
) -> None:
    """Register a per-skill rules-tier handler.

    The skill's runner.py imports this module and calls
    register_rules_handler at import time so the handler is ready
    when run_cascade walks the cascade.

    Idempotent: re-registering with the same key replaces.
    """
    _RULES_HANDLERS[(workflow_id, tier_id)] = handler


def _registered_rules_handler(
    workflow_id: str, tier_id: str,
) -> Optional[Callable[[CascadeContext, dict[str, Any]], CascadeStep]]:
    return _RULES_HANDLERS.get((workflow_id, tier_id))


# ─── built-in tier handlers ────────────────────────────────────────


def _handle_rules(
    ctx: CascadeContext, tier: Any,
) -> CascadeStep:
    handler = _registered_rules_handler(ctx.workflow_id, tier.tier_id)
    if handler is None:
        return CascadeStep(
            kind="escalate",
            reason=(
                f"no_rules_handler_registered:{ctx.workflow_id}/{tier.tier_id}"
            ),
        )
    return handler(ctx, dict(tier.config or {}))


def _handle_cloud_llm(
    ctx: CascadeContext, tier: Any,
) -> CascadeStep:
    """Skills always provide a per-tier LLM handler in ctx.extra
    keyed by tier_id (e.g. ctx.extra['classify_handler_fn']).
    The handler receives the full context + tier config and returns
    a CascadeStep — no built-in prompt construction at the framework
    level (per #33a, prompt content is skill-specific).
    """
    key = f"{tier.tier_id}_handler_fn"
    handler = ctx.extra.get(key)
    if handler is None:
        return CascadeStep(
            kind="escalate",
            reason=f"no_llm_handler_for_tier:{tier.tier_id}",
            metadata={"missing_key": key},
        )
    return handler(ctx, dict(tier.config or {}))


def _handle_second_opinion(
    ctx: CascadeContext, tier: Any,
) -> CascadeStep:
    """Wraps the SecondOpinionTier. Skill provides the gate via
    ctx.extra['safety_gate'] (a SecondOpinionTier instance). If
    None, falls through with continue + a 'gate_not_wired' note
    so the existing operator-approval path still gates the send."""

    from app.runtime.ai_stack.tiers.second_opinion import (
        SecondOpinionInput, SecondOpinionTier,
    )

    gate: Optional[SecondOpinionTier] = ctx.extra.get("safety_gate")
    if gate is None:
        return CascadeStep(
            kind="continue",
            reason="gate_not_wired_using_operator_approval_fallback",
            metadata={},
        )

    cfg = dict(tier.config or {})
    purpose = cfg.get("purpose") or ctx.extra.get("purpose", "")
    criteria = cfg.get("criteria_prompt_path") or "safety/default.md"
    producer_kind = cfg.get("producer_tier_kind", "cloud_llm")

    payload = SecondOpinionInput(
        workflow_id=ctx.workflow_id,
        purpose=purpose,
        criteria_prompt_relpath=criteria,
        proposed_input=ctx.inputs,
        proposed_output=ctx.accumulated.get("draft", ctx.accumulated),
        producer_tier_kind=producer_kind,
        prior_attempts=0,
    )
    verdict = gate.evaluate(payload)
    if verdict.verdict == "allow":
        return CascadeStep(
            kind="continue",
            reason="gate_allowed",
            metadata={"gate_verdict": verdict.model_dump(mode="json")},
        )
    if verdict.verdict in ("refuse", "escalate"):
        return CascadeStep(
            kind="escalate",
            reason=f"gate_{verdict.verdict}",
            metadata={"gate_verdict": verdict.model_dump(mode="json")},
        )
    if verdict.verdict == "send_back":
        # Send-back without a producer-LLM retry loop coerces to
        # escalate. Real retry semantics live in a future framework
        # cascade-runner pass once an LLM drafter is wired in
        # place of a deterministic builder.
        return CascadeStep(
            kind="escalate",
            reason="gate_send_back_no_retry_loop_yet",
            metadata={"gate_verdict": verdict.model_dump(mode="json")},
        )
    return CascadeStep(
        kind="escalate",
        reason=f"gate_unknown_verdict:{verdict.verdict}",
    )


def _handle_human(
    ctx: CascadeContext, tier: Any,
) -> CascadeStep:
    """Final-tier handler. Stages a YAML proposal that the operator
    approves via ✅ in sai-eval. The actual send-tool fires when the
    slack handler picks up the ✅ reaction."""

    proposed_dir: Path = ctx.extra.get(
        "proposed_dir",
        Path.home() / ".sai-runtime" / "eval" / "proposed" / ctx.workflow_id,
    )
    thread_id = str(ctx.inputs.get("thread_id", "unknown"))
    proposed_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposed_dir / f"{thread_id}.yaml"

    # Lift well-known accumulated keys into top-level fields so the
    # slack approval handler + operator-readable proposal can find
    # them without parsing nested context. The full accumulated dict
    # is also preserved under `context` for completeness.
    accum = dict(ctx.accumulated)
    proposal_body: dict[str, Any] = {
        "workflow_id": ctx.workflow_id,
        "thread_id": thread_id,
        "proposed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "draft": accum.get("draft", {}),
        "operator_action_required": (
            "React with ✅ in sai-eval to fire the side-effect tools "
            "for this workflow. React with ❌ to discard the proposal."
        ),
    }
    # Promote standard skill-context fields to top level if present.
    for promoted in (
        "course_id", "course_display_name", "course_term",
        "from_address", "ta_emails", "ta_names",
        "classifier_reason", "student_name", "from",
    ):
        if promoted in accum:
            proposal_body[promoted] = accum[promoted]
    # Preserve everything else for audit visibility.
    proposal_body["context"] = accum
    tmp = proposal_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(proposal_body, sort_keys=False), encoding="utf-8")
    tmp.replace(proposal_path)

    return CascadeStep(
        kind="ready_to_propose",
        reason="awaiting_operator_approval_in_sai_eval",
        metadata={"proposal_path": str(proposal_path)},
    )


_BUILTIN_HANDLERS: dict[str, Callable[[CascadeContext, Any], CascadeStep]] = {
    "rules": _handle_rules,
    "cloud_llm": _handle_cloud_llm,
    "local_llm": _handle_cloud_llm,  # same handler shape
    "second_opinion": _handle_second_opinion,
    "human": _handle_human,
}


# ─── the runner ─────────────────────────────────────────────────────


def run_cascade(
    *,
    manifest: Any,                # SkillManifest from app/skills/manifest.py
    inputs: dict[str, Any],
    extra: Optional[dict[str, Any]] = None,
) -> CascadeResult:
    """Walk manifest.cascade in order; short-circuit on first
    no_op / escalate / ready_to_propose. Returns a CascadeResult
    with the audit log + final verdict + (when applicable) the
    staged proposal path."""

    ctx = CascadeContext(
        workflow_id=manifest.identity.workflow_id,
        inputs=dict(inputs),
        extra=dict(extra or {}),
    )
    audit: list[dict[str, Any]] = []

    for tier in manifest.cascade:
        handler = _BUILTIN_HANDLERS.get(tier.kind)
        if handler is None:
            audit.append({
                "tier": tier.tier_id,
                "kind": "escalate",
                "reason": f"no_handler_for_kind:{tier.kind}",
                "metadata_keys": [],
            })
            return CascadeResult(
                workflow_id=ctx.workflow_id,
                final_verdict="escalate",
                final_reason=f"no_handler_for_kind:{tier.kind}",
                proposal_path=None,
                audit_log=audit,
                accumulated=ctx.accumulated,
            )

        step = handler(ctx, tier)
        audit.append({
            "tier": tier.tier_id,
            "kind": step.kind,
            "reason": step.reason,
            "metadata_keys": sorted(step.metadata.keys()),
        })

        if step.kind in ("no_op", "escalate"):
            return CascadeResult(
                workflow_id=ctx.workflow_id,
                final_verdict=step.kind,
                final_reason=step.reason,
                proposal_path=None,
                audit_log=audit,
                accumulated=ctx.accumulated,
            )

        if step.kind == "ready_to_propose":
            return CascadeResult(
                workflow_id=ctx.workflow_id,
                final_verdict="ready_to_propose",
                final_reason=step.reason,
                proposal_path=step.metadata.get("proposal_path"),
                audit_log=audit,
                accumulated=ctx.accumulated,
            )

        # continue → merge metadata into accumulated and proceed
        ctx.accumulated.update(step.metadata)

    # Cascade exhausted without ready_to_propose → escalate.
    return CascadeResult(
        workflow_id=ctx.workflow_id,
        final_verdict="escalate",
        final_reason="cascade_exhausted_no_terminal_tier",
        proposal_path=None,
        audit_log=audit,
        accumulated=ctx.accumulated,
    )
