"""Resolve a fuzzy operator reference (sender name, email, domain,
subject substring) to one or more concrete Gmail messages.

Used by the eval_add apply path: when an operator says "dinika's mail
should be cherry", we need to find the message they meant before we
can append it to ``edge_cases.jsonl``.

Three input shapes:

  - **Email address** (``dinika@cherry.vc``) → Gmail query ``from:<addr>``
  - **Domain** (``cherry.vc``) → Gmail query ``from:<domain>``
  - **Fuzzy** (``dinika`` or ``Dinika Mahtani``) → Gmail query
    ``from:<term>`` (Gmail's from: matches name AND address)

Returns a ``ResolveResult`` with up to N matches; the slack_bot
decides what to do with 0/1/many matches.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.connectors.gmail import GmailAPIConnector
from app.workers.email_models import EmailMessage

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolveCandidate:
    """One Gmail message matching the operator's reference."""

    message_id: str
    thread_id: str
    from_email: str
    from_name: Optional[str]
    subject: str
    snippet: str
    received_at_iso: Optional[str]
    """ISO-8601 timestamp of when Gmail received the message."""

    def short_summary(self) -> str:
        """One-line operator-facing summary for the disambiguation message."""
        when = (self.received_at_iso or "")[:10] or "unknown date"
        return (
            f"{when} — {self.from_name or self.from_email} — "
            f"{self.subject[:80]}"
        )


@dataclass
class ResolveResult:
    """Outcome of trying to resolve a fuzzy reference."""

    query_used: str
    """The Gmail search query we constructed (for audit + debugging)."""

    candidates: list[ResolveCandidate]
    """Up to N matches; ordered most-recent-first per Gmail's default."""

    error: Optional[str] = None
    """Set when resolution itself failed (auth, network, etc.)."""

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def is_unique(self) -> bool:
        return self.count == 1

    @property
    def has_matches(self) -> bool:
        return self.count > 0


def build_query(
    target_pattern: str,
    target_kind: Optional[str] = None,
    *,
    days_back: int = 30,
) -> str:
    """Convert a fuzzy reference into a Gmail search query.

    Defaults to limiting to the last `days_back` days so we don't
    pull a noisy hit from years ago when the operator said "dinika"
    meaning "recent mail from dinika."

    Supported `target_kind` values:
      * ``sender_email``  → ``from:<addr>``
      * ``sender_domain`` → ``from:<domain>``
      * ``subject``       → ``subject:"<text>"``
      * ``free_text``     → broad search across body + subject + from
      * ``None`` (auto)   → infer from pattern (presence of @ → email,
                            looks-like-domain → domain, else fuzzy
                            from-name match)
    """

    target = (target_pattern or "").strip()
    if not target:
        raise ValueError("empty target_pattern; can't build a Gmail query")

    # Strip leading "@" first so "@example.com" and "example.com" behave
    # the same way at the routing decision below.
    if target.startswith("@"):
        target = target[1:]

    if target_kind == "subject":
        # Subject-line search. Quote so multi-word subjects don't break.
        clause = f'subject:"{target}"'
    elif target_kind == "free_text":
        # Broad search — anywhere in the message. Catches "Security
        # alert for testcornellstudenttest@gmail.com" appearing in a
        # subject OR body OR from-display-name.
        clause = f'"{target}"' if " " in target else target
    elif "@" in target or target_kind == "sender_email":
        # Specific email address — exact match.
        clause = f"from:{target}"
    elif target_kind == "sender_domain" or _looks_like_domain(target):
        # Domain — Gmail's from: with @<domain> matches.
        clause = f"from:{target}"
    else:
        # Fuzzy free-text — let Gmail's from: do the fuzzy match.
        # Quote it if there's a space (e.g. "Dinika Mahtani").
        if " " in target:
            clause = f'from:"{target}"'
        else:
            clause = f"from:{target}"

    # Date filter so old hits don't drown out recent context.
    if days_back > 0:
        clause = f"{clause} newer_than:{days_back}d"

    return clause


def resolve_with_fallback(
    target_pattern: str,
    *,
    target_kind: Optional[str] = None,
    authenticator: Any,
    user_id: str = "me",
    max_results: int = 5,
    days_back: int = 30,
) -> ResolveResult:
    """Try the operator's hint first; fall back to subject + free_text
    if the first try returns 0 candidates.

    This is the agent's preferred entry point. The agent should call
    this BEFORE asking the operator for a more specific search term —
    the resolver does the obvious next-tries autonomously, so the
    agent only has to ask when even the broad search fails.

    Search order:
      1. operator's hint (if `target_kind` provided) — exact intent
      2. subject:"<target>"  — operator may have given a subject phrase
      3. free_text search    — last-resort full-message search

    Stops at the first non-empty result. Each step is logged so the
    audit trail shows what was tried.
    """

    attempts: list[str] = []

    # Step 1: original hint (or auto-inferred kind).
    first = resolve(
        target_pattern, target_kind=target_kind,
        authenticator=authenticator, user_id=user_id,
        max_results=max_results, days_back=days_back,
    )
    attempts.append(first.query_used)
    if first.has_matches or first.error:
        return first

    # Step 2: subject search (skip if operator already specified).
    if target_kind != "subject":
        sub = resolve(
            target_pattern, target_kind="subject",
            authenticator=authenticator, user_id=user_id,
            max_results=max_results, days_back=days_back,
        )
        attempts.append(sub.query_used)
        if sub.has_matches or sub.error:
            return sub

    # Step 3: free-text search.
    if target_kind != "free_text":
        ft = resolve(
            target_pattern, target_kind="free_text",
            authenticator=authenticator, user_id=user_id,
            max_results=max_results, days_back=days_back,
        )
        attempts.append(ft.query_used)
        if ft.has_matches or ft.error:
            return ft

    # All three failed — return the original empty result with the
    # full attempts list in the query_used field for audit.
    return ResolveResult(
        query_used=" | tried also: ".join(attempts),
        candidates=[], error=None,
    )


def _looks_like_domain(s: str) -> bool:
    """Heuristic: contains a dot, no @, no spaces, ASCII-only TLD-ish."""
    if "@" in s or " " in s:
        return False
    if not re.search(r"\.[a-zA-Z]{2,}$", s):
        return False
    return True


def resolve(
    target_pattern: str,
    *,
    target_kind: Optional[str] = None,
    authenticator: Any,
    user_id: str = "me",
    max_results: int = 5,
    days_back: int = 30,
) -> ResolveResult:
    """Search Gmail for messages matching the operator's reference.

    `authenticator` is a configured ``GmailOAuthAuthenticator``. The
    resolver uses ``GmailAPIConnector`` under the hood so it inherits
    the operator's existing OAuth + read scopes.
    """

    try:
        query = build_query(target_pattern, target_kind, days_back=days_back)
    except ValueError as exc:
        return ResolveResult(query_used="", candidates=[], error=str(exc))

    LOGGER.info("gmail_message_resolver: query=%r", query)

    connector = GmailAPIConnector(
        authenticator=authenticator,
        user_id=user_id,
        query=query,
        label_ids=[],  # search across all labels, not just INBOX
        max_results=max_results,
    )
    try:
        messages: list[EmailMessage] = connector.fetch_messages()
    except Exception as exc:
        LOGGER.exception("gmail_message_resolver: fetch failed")
        return ResolveResult(
            query_used=query, candidates=[],
            error=f"Gmail fetch failed: {type(exc).__name__}: {exc}",
        )

    candidates = [
        ResolveCandidate(
            message_id=m.message_id,
            thread_id=m.thread_id or m.message_id,
            from_email=m.from_email,
            from_name=m.from_name,
            subject=m.subject or "(no subject)",
            snippet=m.snippet or "",
            received_at_iso=(
                m.received_at.isoformat() if m.received_at else None
            ),
        )
        for m in messages
    ]
    return ResolveResult(query_used=query, candidates=candidates)
