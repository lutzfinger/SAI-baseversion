"""Structured reply draft + safety validators (PRINCIPLES.md §6 + §16f).

Every auto-reply produced by a skill (e1, future) flows through
this module before the send tool fires. Structure beats string
checks: a Pydantic model with field validators rejects unsafe
drafts at construction time, so the runner can't accidentally send
an invalid one.

Validators (all hard-fail):

  1. ``must_self_identify_as_ai`` — body MUST mention "AI", "SAI",
     or "AI assistant" so the recipient knows they're not talking
     to a human.
  2. ``must_not_promise_extension`` — bans patterns that imply a
     commitment ("I will give you", "you have an extension",
     "definitely", "guarantee").
  3. ``length_in_bounds`` — 200-2000 chars; outside → invalid (huge
     body suggests template error, tiny body suggests render bug).
  4. ``cc_list_present_and_well_formed`` — CC list must be non-
     empty and every entry must look like an email.
  5. ``no_other_student_names`` — opt-in; checks against an
     operator-provided roster, refuses if any roster name appears
     uninvited in the body.

REMOVED 2026-05-04: ``tone_appropriate_for_classification``. The
validator used to ban auto-empathy phrases on no_exception drafts.
Operator's revised judgment: warm acknowledgement is the right
tone for ALL student-facing replies, even routine cases. See
``docs/design_reply_validator_loosen.md`` for the design + risk
assessment.

Intent: this is a SAFETY layer, distinct from the second-opinion
gate. The gate is an LLM judging output; this is deterministic
schema enforcement. Both fire before send. Fail-closed per #6.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.canonical.patterns import EMAIL_RE as _EMAIL_RE


_AI_IDENT_RE = re.compile(r"\b(AI|SAI|AI assistant)\b", re.IGNORECASE)

_PROMISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bi (will|shall) (give|grant|approve|extend)\b", re.IGNORECASE),
    re.compile(r"\byou (have|now have) (an?|the) extension\b", re.IGNORECASE),
    re.compile(r"\bdefinitely\b", re.IGNORECASE),
    re.compile(r"\bguarantee\b", re.IGNORECASE),
    re.compile(r"\bpromise\b", re.IGNORECASE),
    re.compile(r"\bapproved\b", re.IGNORECASE),
    re.compile(r"\bgranted\b", re.IGNORECASE),
]

class ReplyDraft(BaseModel):
    """Validated SAI-authored reply draft.

    All side-effecting tools should accept ReplyDraft, not raw strings.
    Construction failure = the draft is unsafe; the runner refuses
    to send.
    """

    model_config = ConfigDict(extra="forbid")

    classification: str = Field(
        ..., description=(
            "The skill's verdict that produced this draft. Used by "
            "tone validator: 'no_exception' MUST NOT use auto-empathy "
            "phrases."
        ),
    )
    to: str = Field(..., min_length=5)
    cc: list[str] = Field(default_factory=list)
    subject: str = Field(..., min_length=1, max_length=300)
    body: str = Field(..., min_length=200, max_length=2000)
    other_student_names: list[str] = Field(
        default_factory=list,
        description="Optional roster of OTHER students whose names "
                    "MUST NOT appear in body. When provided, "
                    "no_other_student_names validator runs.",
    )

    @field_validator("to", mode="after")
    @classmethod
    def _to_well_formed(cls, v: str) -> str:
        if not _EMAIL_RE.match(v.strip()):
            raise ValueError(f"to must look like an email: {v!r}")
        return v.strip()

    @field_validator("cc", mode="after")
    @classmethod
    def _cc_well_formed(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("cc list must not be empty")
        for addr in v:
            if not _EMAIL_RE.match(addr.strip()):
                raise ValueError(f"cc entry not an email: {addr!r}")
        return [a.strip() for a in v]

    @field_validator("body", mode="after")
    @classmethod
    def _must_self_identify_as_ai(cls, v: str) -> str:
        if not _AI_IDENT_RE.search(v):
            raise ValueError(
                "body must self-identify as AI / SAI / AI assistant"
            )
        return v

    @field_validator("body", mode="after")
    @classmethod
    def _must_not_promise_extension(cls, v: str) -> str:
        for pat in _PROMISE_PATTERNS:
            if pat.search(v):
                raise ValueError(
                    f"body contains promise-language pattern: {pat.pattern!r}"
                )
        return v

    @model_validator(mode="after")
    def _no_other_student_names(self) -> "ReplyDraft":
        if not self.other_student_names:
            return self
        body_lower = self.body.lower()
        for name in self.other_student_names:
            n = name.strip().lower()
            if n and n in body_lower:
                raise ValueError(
                    f"body contains another student's name: {name!r}"
                )
        return self

