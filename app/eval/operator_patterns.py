"""Pre-registered safe patterns for operator-driven Slack edits.

Per PRINCIPLES.md §16b: operator-driven changes to the eval set OR to the
rules tier arrive through `#sai-eval`. The five gates from #9 apply
(channel-bound, identity-bound, pattern-bound, two-phase committed,
hash-aware). This module owns the **pattern-bound** layer — the parsers
that recognise legitimate operator instructions and reject everything
else.

Pre-registered patterns:

  ``parse_add_rule(text)``   — "add rule: from <sender|domain> → L1/<bucket>"
                                Loop 4 classifier change.
  ``parse_add_eval(text)``   — "<message_ref> should have been L1/<bucket>"
                                Loop 4 LLM hint.

Both return a Pydantic-validated proposal that the slack_bot stages to
disk; the actual apply is via /sai-checkin or operator-reaction (slice v2+).

Tolerant of formatting variations the operator is likely to type:

  - Unicode arrow → or ASCII -> or =>
  - Bucket name in any case ("Cherry", "customers", "L1/Cherry", "customers")
  - "from <X>" optional ("add rule: acme@example.com → customers" works)
  - Singular/plural forgiveness ("customer" → "customers" via the same
    map used by merge_curated_eval_into_overlay)

Returns None on no-match (safe; operator's message wasn't an instruction).
Raises ParseError on partial-match-with-bad-args (loud; operator typo
that we can clarify in Slack).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.workers.email_models import (
    LEVEL1_DISPLAY_NAMES,
    Level1Classification,
)

VALID_L1: set[str] = set(get_args(Level1Classification))
_DISPLAY_TO_BUCKET: dict[str, str] = {
    v.lower(): k for k, v in LEVEL1_DISPLAY_NAMES.items()
}
_SINGULAR_TO_PLURAL: dict[str, str] = {
    "customer": "customers",
    "newsletter": "newsletters",
    "invoice": "invoices",
    "update": "updates",
    "friend": "friends",
}

# Arrow forms operators commonly type. Order matters — match longer first.
_ARROW = r"(?:->|=>|→|\bto\b|\bas\b)"

# add rule: [from] <sender_or_domain> → L1/<bucket>  (or just bucket)
_ADD_RULE_RE = re.compile(
    r"""^\s*
        add\s+rule\s*[:=]?\s*           # "add rule:" / "add rule"
        (?:from\s+)?                    # optional "from "
        (?P<target>\S+?)                # sender / domain (no spaces)
        \s*""" + _ARROW + r"""\s*       # arrow
        (?:L1[/:])?                     # optional "L1/"
        (?P<bucket>[a-zA-Z_]+)          # bucket name
        \s*\.?\s*$                      # optional trailing punctuation
    """,
    re.IGNORECASE | re.VERBOSE,
)

# <message_ref> should have been L1/<bucket>
# message_ref can be a Gmail URL, a message_id, or a quoted subject prefix.
_ADD_EVAL_RE = re.compile(
    r"""^\s*
        (?P<ref>.+?)                    # message reference (greedy minimal)
        \s+should\s+(?:have\s+been|be)\s+
        (?:L1[/:])?
        (?P<bucket>[a-zA-Z_]+)
        \s*\.?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ParseError(ValueError):
    """Raised when a pattern partial-matches but arguments are bad
    (e.g., bucket isn't a valid L1). The slack_bot can echo a
    clarification message rather than silently ignoring."""


class AddRuleProposal(BaseModel):
    """Operator wants to add a sender/domain → L1 rule to the rules tier."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    proposed_at: datetime
    proposed_by: str
    """Slack user_id of the operator (validated against the configured
    operator_user_id by the slack_bot before this proposal is built)."""

    target: str
    """Sender email (e.g. ``acme@example.com``) or domain (e.g. ``example.com``)."""

    target_kind: Literal["sender_email", "sender_domain"]
    """Inferred from the target string — presence of ``@`` indicates email."""

    expected_level1_classification: Level1Classification

    source_text: str
    """The operator's original message text (for audit + debugging)."""


class AddEvalProposal(BaseModel):
    """Operator wants to record an email's correct L1 in the LLM eval set."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    proposed_at: datetime
    proposed_by: str

    message_ref: str
    """The operator's original reference text — e.g. ``dinika``,
    ``Dinika Mahtani``, or a Gmail URL. Audit-only after resolution."""

    expected_level1_classification: Level1Classification

    source_text: str

    # ── Resolved fields (populated by the v2b disambiguation flow before
    # staging). Optional only because parse_add_eval emits the proposal
    # before resolution; the apply path validates they are present.
    resolved_message_id: Optional[str] = None
    resolved_thread_id: Optional[str] = None
    resolved_from_email: Optional[str] = None
    resolved_from_name: Optional[str] = None
    resolved_subject: Optional[str] = None
    resolved_snippet: Optional[str] = None
    resolved_received_at_iso: Optional[str] = None


# ─── parsers ──────────────────────────────────────────────────────────────


def _normalise_bucket(raw: str) -> Optional[str]:
    s = raw.strip().lower()
    if s in VALID_L1:
        return s
    if s in _DISPLAY_TO_BUCKET:
        return _DISPLAY_TO_BUCKET[s]
    if s in _SINGULAR_TO_PLURAL:
        candidate = _SINGULAR_TO_PLURAL[s]
        if candidate in VALID_L1:
            return candidate
    return None


def parse_add_rule(text: str, *, proposed_by: str) -> Optional[AddRuleProposal]:
    """Try to parse `text` as an "add rule" instruction.

    Returns:
      - ``AddRuleProposal`` on a clean match
      - ``None`` if `text` doesn't look like an "add rule" instruction at
        all (operator was just chatting; safe to ignore)
      - raises ``ParseError`` if `text` matches the prefix but the bucket
        is invalid (operator typo'd; bot should reply with a hint)
    """

    if not text or "add rule" not in text.lower():
        return None
    m = _ADD_RULE_RE.match(text)
    if m is None:
        raise ParseError(
            "I think you want to add a rule but I couldn't parse the rest. "
            "Try something like: `add rule: acme@example.com → customers`"
        )
    target = m.group("target").strip().lower()
    bucket_raw = m.group("bucket")
    bucket = _normalise_bucket(bucket_raw)
    if bucket is None:
        raise ParseError(
            f"`{bucket_raw}` isn't one of my labels. "
            f"Try: {', '.join(sorted(VALID_L1))}"
        )

    target_kind: Literal["sender_email", "sender_domain"] = (
        "sender_email" if "@" in target else "sender_domain"
    )

    return AddRuleProposal(
        proposal_id=_proposal_id("rule_add", target),
        proposed_at=datetime.now(UTC),
        proposed_by=proposed_by,
        target=target,
        target_kind=target_kind,
        expected_level1_classification=bucket,  # type: ignore[arg-type]
        source_text=text,
    )


def parse_add_eval(text: str, *, proposed_by: str) -> Optional[AddEvalProposal]:
    """Try to parse `text` as a "<message_ref> should have been L1/<bucket>"
    instruction.

    Returns AddEvalProposal on match, None on no match, raises ParseError
    on partial match.
    """

    if not text or "should" not in text.lower():
        return None
    m = _ADD_EVAL_RE.match(text)
    if m is None:
        return None  # too lenient to raise; "should" appears in many messages
    ref = m.group("ref").strip()
    bucket_raw = m.group("bucket")
    bucket = _normalise_bucket(bucket_raw)
    if bucket is None:
        raise ParseError(
            f"`{bucket_raw}` isn't one of my labels. "
            f"Try: {', '.join(sorted(VALID_L1))}"
        )
    return AddEvalProposal(
        proposal_id=_proposal_id("eval_add", ref),
        proposed_at=datetime.now(UTC),
        proposed_by=proposed_by,
        message_ref=ref,
        expected_level1_classification=bucket,  # type: ignore[arg-type]
        source_text=text,
    )


def _proposal_id(kind: str, slug: str) -> str:
    """Stable proposal id of the form ``<kind>::<ts>::<slug>``."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_slug = re.sub(r"[^a-zA-Z0-9._-]", "_", slug)[:60]
    return f"{kind}::{ts}::{safe_slug}"
