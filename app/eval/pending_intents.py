"""Pending-intent state machine — the missing piece that keeps an
operator interaction alive across multiple turns until it reaches
closure.

Per PRINCIPLES.md §16g: an operator trigger creates a pending intent
that persists until ONE of:
  - operator approves a proposal under it (✅) → status=resolved
  - operator explicitly drops it → status=dropped
  - intent goes idle past INTENT_IDLE_TIMEOUT_HOURS → status=expired

The intent is NEVER dropped by:
  - rejection (❌) on a staged proposal → mid-flight feedback
  - bot uncertainty → ask again
  - failed apply gate → surface + ask for different shape

The intent's history (proposals tried, rejections with reasons,
operator comments) becomes the agent's context on next turn so the
agent can adjust shape rather than repeat the same wrong proposal.

File-backed store at ``eval/pending_intents/<intent_id>.json``.
NOT in PRESERVE_ON_CLEAN — operator data, gets re-merged from
private overlay.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger(__name__)


from app.shared.runtime_tunables import get as _tunable


def _idle_timeout_hours() -> int:
    """Operator-tunable via config/sai_runtime_tunables.yaml. Default 96
    (4 days) per operator decision 2026-05-03."""
    return int(_tunable("intent_idle_timeout_hours"))


def _max_history_events() -> int:
    return int(_tunable("intent_max_history_events"))


# Back-compat constants — read from runtime tunables on access. Existing
# callers can still reference these names; tests can monkeypatch the
# tunables loader for deterministic behavior.
INTENT_IDLE_TIMEOUT_HOURS: int = _idle_timeout_hours()
MAX_HISTORY_EVENTS_PER_INTENT: int = _max_history_events()


IntentStatus = Literal["open", "resolved", "dropped", "expired"]
IntentEventKind = Literal[
    "operator_message",   # operator typed something
    "agent_proposal",     # bot proposed a change (staged YAML)
    "operator_rejection", # operator reacted ❌
    "operator_approval",  # operator reacted ✅
    "operator_comment",   # operator's free-text reply in the thread
    "bot_clarification",  # bot asked for more info
    "intent_resolved",
    "intent_dropped",
    "intent_expired",
]


class IntentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    kind: IntentEventKind
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingIntent(BaseModel):
    """One operator interaction tracked from open to closure."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str
    """Stable id of the form ``intent_<ts>_<short_random>``."""

    thread_ts: str
    """Slack thread anchor — the operator's original top-level msg ts."""

    channel: str
    operator_user_id: str

    original_text: str
    """The operator's first message; used as the agent's seed input."""

    status: IntentStatus = "open"
    created_at: datetime
    last_activity_at: datetime

    history: list[IntentEvent] = Field(default_factory=list)

    closure_reason: Optional[str] = None
    """Set when status moves to resolved/dropped/expired."""

    def is_idle_past(self, hours: int) -> bool:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return self.last_activity_at < cutoff

    def append(self, kind: IntentEventKind, text: str = "", **metadata: Any) -> None:
        """Append a history event + bump last_activity_at."""

        self.history.append(IntentEvent(
            at=datetime.now(UTC),
            kind=kind,
            text=text[:1000],
            metadata=metadata,
        ))
        # Cap history to the last N events to prevent runaway growth.
        if len(self.history) > MAX_HISTORY_EVENTS_PER_INTENT:
            self.history = self.history[-MAX_HISTORY_EVENTS_PER_INTENT:]
        self.last_activity_at = datetime.now(UTC)

    def prior_proposals_summary(self) -> str:
        """Human-readable summary of proposals tried + rejection reasons.
        Used as agent context on re-invocation so it can avoid repeating
        the same wrong shape."""

        lines: list[str] = []
        for ev in self.history:
            if ev.kind == "agent_proposal":
                lines.append(f"- Proposed: {ev.text}")
            elif ev.kind == "operator_rejection":
                reason = ev.text or "(no reason given)"
                lines.append(f"- Operator rejected with reason: {reason}")
            elif ev.kind == "operator_comment":
                lines.append(f"- Operator added: {ev.text}")
            elif ev.kind == "bot_clarification":
                lines.append(f"- Bot asked: {ev.text}")
        return "\n".join(lines) if lines else "(no prior attempts)"


# ─── store ────────────────────────────────────────────────────────────


class PendingIntentStore:
    """File-backed store at ``<root>/eval/pending_intents/``.

    One JSON file per intent. Reads/writes are not atomic across
    processes; the slack_bot is single-process so contention is OK.
    Operator hand-edits are tolerated (re-read on next access).
    """

    def __init__(self, root: Path):
        self.root = root / "eval" / "pending_intents"

    def _path_for(self, intent_id: str) -> Path:
        safe_id = intent_id.replace("/", "_").replace("..", "_")
        return self.root / f"{safe_id}.json"

    def open_intent(
        self, *,
        thread_ts: str,
        channel: str,
        operator_user_id: str,
        original_text: str,
    ) -> PendingIntent:
        """Create + persist a new open intent."""

        intent = PendingIntent(
            intent_id=_new_intent_id(),
            thread_ts=thread_ts,
            channel=channel,
            operator_user_id=operator_user_id,
            original_text=original_text,
            status="open",
            created_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
        )
        intent.append("operator_message", text=original_text)
        self._save(intent)
        return intent

    def find_by_thread_ts(self, thread_ts: str) -> Optional[PendingIntent]:
        """Return the OPEN intent anchored at this thread, or None."""

        if not thread_ts:
            return None
        if not self.root.exists():
            return None
        for path in self.root.glob("*.json"):
            try:
                intent = PendingIntent.model_validate_json(path.read_text())
            except Exception:
                continue
            if intent.thread_ts == thread_ts and intent.status == "open":
                return intent
        return None

    def find_by_intent_id(self, intent_id: str) -> Optional[PendingIntent]:
        path = self._path_for(intent_id)
        if not path.exists():
            return None
        try:
            return PendingIntent.model_validate_json(path.read_text())
        except Exception:
            return None

    def append_event(
        self, intent_id: str, kind: IntentEventKind,
        text: str = "", **metadata: Any,
    ) -> Optional[PendingIntent]:
        """Read + append + write. Returns updated intent or None if missing."""

        intent = self.find_by_intent_id(intent_id)
        if intent is None:
            return None
        intent.append(kind, text=text, **metadata)
        self._save(intent)
        return intent

    def resolve(self, intent_id: str, *, reason: str = "") -> Optional[PendingIntent]:
        return self._close(intent_id, status="resolved", reason=reason)

    def drop(self, intent_id: str, *, reason: str = "") -> Optional[PendingIntent]:
        return self._close(intent_id, status="dropped", reason=reason)

    def expire(self, intent_id: str, *, reason: str = "idle timeout") -> Optional[PendingIntent]:
        return self._close(intent_id, status="expired", reason=reason)

    def _close(
        self, intent_id: str, *,
        status: IntentStatus, reason: str,
    ) -> Optional[PendingIntent]:
        intent = self.find_by_intent_id(intent_id)
        if intent is None:
            return None
        if intent.status != "open":
            return intent  # already closed; idempotent
        intent.status = status
        intent.closure_reason = reason or None
        kind: IntentEventKind = (
            "intent_resolved" if status == "resolved"
            else "intent_dropped" if status == "dropped"
            else "intent_expired"
        )
        intent.append(kind, text=reason)
        self._save(intent)
        return intent

    def open_intents(self) -> list[PendingIntent]:
        """All currently-open intents (for sweep + observability)."""

        if not self.root.exists():
            return []
        out: list[PendingIntent] = []
        for path in self.root.glob("*.json"):
            try:
                intent = PendingIntent.model_validate_json(path.read_text())
                if intent.status == "open":
                    out.append(intent)
            except Exception:
                continue
        return out

    def sweep_idle(
        self, *, max_age_hours: int = INTENT_IDLE_TIMEOUT_HOURS,
    ) -> list[PendingIntent]:
        """Mark all open intents that have been idle > max_age_hours
        as expired. Returns the swept-out intents so the caller can
        post a final note to each thread.
        """

        expired: list[PendingIntent] = []
        for intent in self.open_intents():
            if intent.is_idle_past(max_age_hours):
                closed = self._close(
                    intent.intent_id, status="expired",
                    reason=f"idle for >{max_age_hours}h",
                )
                if closed is not None:
                    expired.append(closed)
        return expired

    def _save(self, intent: PendingIntent) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(intent.intent_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(intent.model_dump_json(indent=2))
        tmp.replace(path)


def _new_intent_id() -> str:
    return f"intent_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


# ─── agent context helper ─────────────────────────────────────────────


def build_agent_context_from_intent(intent: PendingIntent) -> str:
    """Format an intent's history into agent-facing context.

    Used when re-invoking the agent under an OPEN intent: this becomes
    the additional context that says "you previously proposed X; the
    operator said no because Y; try a different shape."
    """

    if not intent.history or len(intent.history) <= 1:
        return ""

    lines = [
        "── Prior attempts in this conversation ──",
        f"Original operator request: {intent.original_text}",
        "",
        "What's happened so far:",
        intent.prior_proposals_summary(),
        "",
        "Important: the operator is still waiting for closure on the "
        "original request. Do NOT repeat any rejected proposal "
        "unchanged. If they rejected a CLASSIFIER RULE because it was "
        "too broad, try an LLM EXAMPLE for the specific email instead "
        "(or vice versa). If you're unsure, ASK what shape they want "
        "before proposing again.",
    ]
    return "\n".join(lines)
