"""Second-opinion gate tier (PRINCIPLES.md §16f / §10 / design doc).

A watchdog LLM that judges whether an upstream tier's proposed
output is safe to apply. Verdict set:

  * allow      — proposed output passes; cascade proceeds to side
                 effects.
  * refuse     — clear violation; never apply; post to operator.
  * escalate   — gate isn't confident; route to next tier (typically
                 human).
  * send_back  — output is CLOSE but not quite right; the producing
                 LLM should retry with the gate's critique. Single-
                 shot per cascade walk; the runner coerces a 2nd
                 send_back to escalate.

Design constraints (per the operator's 2026-05-04 refinement):

  * The gate has NO tools. It sees only the proposed input + output
    + the workflow's purpose statement (from #16i registry) + the
    criteria_prompt (hash-locked file per #24c).
  * The gate NEVER writes new code, instructions, or output. Its
    `reasoning` field is freeform critique text only. The cascade
    runner concatenates it into the producing LLM's retry prompt.
  * Per-channel risk_class drives which LLM role this tier uses
    (medium → safety_gate_medium / local; high → safety_gate_high /
    cloud).
  * Fail-closed: malformed Provider output, timeout, cost-cap
    exceeded → verdict = "escalate" (never "allow").

This module is deliberately stateless: the cascade runner owns the
``prior_attempts`` counter and the retry loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.prompt_loader import PromptHashMismatch, load_hashed_prompt

LOGGER = logging.getLogger(__name__)

VerdictLiteral = Literal["allow", "escalate", "refuse", "send_back"]


class SecondOpinionVerdict(BaseModel):
    """One verdict from the gate."""

    model_config = ConfigDict(extra="forbid")

    verdict: VerdictLiteral
    reasoning: str = Field(
        default="",
        description=(
            "Freeform critique text. For send_back this becomes the "
            "'Reviewer note:' block fed to the producing LLM on retry."
        ),
    )
    triggers: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    gate_prompt_sha256: str = Field(
        default="",
        description="Hash of the criteria_prompt file. Lets a future "
                    "auditor reconstruct the exact gate behavior.",
    )

    @field_validator("reasoning", mode="after")
    @classmethod
    def _strip_reasoning(cls, v: str) -> str:
        # Truncate runaway reasoning to keep retry prompts bounded.
        return v.strip()[:2000]


@dataclass
class SecondOpinionInput:
    """Bundle the gate sees."""

    workflow_id: str
    purpose: str                # from channel registry topic.description
    criteria_prompt_relpath: str  # hash-locked path under prompts/
    proposed_input: dict[str, Any]
    proposed_output: dict[str, Any]
    producer_tier_kind: str       # so the gate knows if send_back is valid
    prior_attempts: int = 0


class SecondOpinionTier:
    """Stateless gate tier. Runner owns the retry counter."""

    tier_kind = "second_opinion"

    def __init__(
        self,
        *,
        tier_id: str,
        provider: Any,
        confidence_threshold: float = 0.85,
        cost_cap_per_call_usd: float = 0.01,
    ) -> None:
        self.tier_id = tier_id
        self.provider = provider
        self.confidence_threshold = confidence_threshold
        self.cost_cap_per_call_usd = cost_cap_per_call_usd

    def evaluate(self, payload: SecondOpinionInput) -> SecondOpinionVerdict:
        """Run the gate ONCE. Returns a verdict.

        Coercion rules:
          * If the producing tier was deterministic (rules / classifier),
            send_back is invalid → coerce to escalate.
          * If prior_attempts >= 1 and the model returns send_back →
            coerce to escalate (runner enforces single-shot).
          * If anything fails (Provider error, malformed JSON, hash
            mismatch, cost cap), return escalate. Fail-closed per #6.
        """

        try:
            criteria_prompt = load_hashed_prompt(payload.criteria_prompt_relpath)
        except PromptHashMismatch as exc:
            LOGGER.warning("gate prompt hash mismatch: %s", exc)
            return SecondOpinionVerdict(
                verdict="escalate",
                reasoning="gate_criteria_prompt_failed_hash_verification",
                triggers=["gate_setup_error"],
                confidence=0.0,
            )

        rendered = _render_envelope(
            purpose=payload.purpose,
            criteria_prompt=criteria_prompt,
            proposed_input=payload.proposed_input,
            proposed_output=payload.proposed_output,
            prior_attempts=payload.prior_attempts,
        )

        # Per PRINCIPLES.md §6a — strict schema enforcement at the
        # API layer. The Anthropic API rejects values outside the
        # verdict enum upstream of model_validate, so we never see
        # malformed verdicts here (the catch-all malformed branch
        # below stays as defense in depth).
        gate_schema = {
            "type": "object",
            "required": ["verdict", "reasoning", "triggers", "confidence"],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["allow", "escalate", "refuse", "send_back"],
                },
                "reasoning": {"type": "string", "maxLength": 2000},
                "triggers": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0,
                },
                "gate_prompt_sha256": {"type": "string"},
            },
            "additionalProperties": False,
        }

        try:
            # Older test stubs may not accept a schema kwarg; degrade
            # gracefully (still get the dict, just without API-layer
            # schema enforcement — the model_validate call below is
            # the second-line defense).
            try:
                raw = self.provider.predict_json(
                    rendered, schema=gate_schema,
                    schema_name="SecondOpinionVerdict",
                )
            except TypeError:
                raw = self.provider.predict_json(rendered)
        except Exception as exc:
            LOGGER.warning("gate provider error: %s", exc)
            return SecondOpinionVerdict(
                verdict="escalate",
                reasoning=f"gate_provider_error:{type(exc).__name__}",
                triggers=["gate_provider_error"],
                confidence=0.0,
            )

        try:
            verdict = SecondOpinionVerdict.model_validate(raw)
        except Exception as exc:
            LOGGER.warning("gate output malformed: %s", exc)
            return SecondOpinionVerdict(
                verdict="escalate",
                reasoning=f"gate_output_malformed:{type(exc).__name__}",
                triggers=["gate_output_malformed"],
                confidence=0.0,
            )

        # Coerce send_back to escalate when invalid.
        coerced_reason = _coerce_send_back_if_invalid(
            verdict.verdict,
            producer_tier_kind=payload.producer_tier_kind,
            prior_attempts=payload.prior_attempts,
        )
        if coerced_reason is not None:
            verdict = SecondOpinionVerdict(
                verdict="escalate",
                reasoning=(
                    f"send_back coerced to escalate: {coerced_reason}. "
                    f"Original critique: {verdict.reasoning}"
                ),
                triggers=verdict.triggers + ["send_back_coerced"],
                confidence=verdict.confidence,
            )

        return verdict


def _coerce_send_back_if_invalid(
    proposed_verdict: str,
    *,
    producer_tier_kind: str,
    prior_attempts: int,
) -> Optional[str]:
    """Return a coercion reason iff send_back must be coerced to escalate."""

    if proposed_verdict != "send_back":
        return None
    if prior_attempts >= 1:
        return "single_shot_rule"
    if producer_tier_kind in {"rules", "classifier"}:
        return "deterministic_producer_no_retry_meaningful"
    return None


def _render_envelope(
    *,
    purpose: str,
    criteria_prompt: str,
    proposed_input: dict[str, Any],
    proposed_output: dict[str, Any],
    prior_attempts: int,
) -> str:
    """Build the gate's user-message body. The system role / tool
    config goes in the Provider; this is just the content."""

    return (
        "You are a safety reviewer. The workflow's purpose is:\n\n"
        f"PURPOSE:\n{purpose}\n\n"
        "You are reviewing one proposed action against these criteria:\n\n"
        f"CRITERIA:\n{criteria_prompt}\n\n"
        "PROPOSED INPUT:\n"
        f"{json.dumps(proposed_input, indent=2, default=str)}\n\n"
        "PROPOSED OUTPUT:\n"
        f"{json.dumps(proposed_output, indent=2, default=str)}\n\n"
        f"PRIOR ATTEMPTS THIS TASK: {prior_attempts}\n\n"
        "Return JSON in this exact shape (no other output):\n"
        "{\n"
        '  "verdict": "allow" | "escalate" | "refuse" | "send_back",\n'
        '  "reasoning": "<1-2 sentences; for send_back this is the critique '
        'the producing LLM will see on retry>",\n'
        '  "triggers": ["<criterion that fired>", ...],\n'
        '  "confidence": <float 0-1>\n'
        "}\n\n"
        "Hard rules:\n"
        '- "refuse" = unsafe under any interpretation.\n'
        '- "escalate" = a human needs to look; gate not confident.\n'
        '- "send_back" = output is CLOSE but not quite right. Only valid\n'
        "  when prior_attempts == 0 AND the producing tier was an LLM.\n"
        "  You do NOT write the new output yourself — only describe what\n"
        "  is wrong so the producing LLM can fix it.\n"
        '- "allow" = nothing concerning; proceed.\n'
        "- When in doubt, escalate.\n\n"
        "NEVER write replacement output, code, or instructions. Verdict\n"
        "+ critique only. The runner controls retry mechanics."
    )


def build_retry_prompt(
    *,
    original_prompt: str,
    original_output: str,
    gate_reasoning: str,
) -> str:
    """Helper for the cascade runner: assemble the producer-LLM
    retry prompt when the gate said send_back. Concatenation only —
    no LLM-side instruction injection beyond the labels.
    """

    return (
        f"{original_prompt}\n\n"
        "── Previous attempt ──\n"
        f"{original_output}\n\n"
        "── Reviewer note ──\n"
        f"{gate_reasoning}\n\n"
        "Please retry with the reviewer's critique addressed."
    )
