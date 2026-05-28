"""email_intents — per-thread conversation state for the email runner.

When the operator emails the bot, the bot may need to ask follow-up
questions (clarify which customer? approve the plan?). The conversation
unfolds across multiple round-trips on the SAME email thread. Per SAI
principle #16g (pending intents — never drop silently), each open
exchange persists on disk until one of the three closure events:
  1. operator approves (terminal state: COMPLETED)
  2. operator drops/cancels (terminal state: DROPPED)
  3. idle timeout exceeds INTENT_IDLE_TIMEOUT_HOURS (terminal: EXPIRED)

State file shape (one JSON per thread):
  ~/Library/Application Support/SAI/receipt-collector/email_intents/<thread_id>.json

Each file:
{
  "thread_id": "abc123",
  "status": "AWAITING_CLARIFICATION"|"AWAITING_APPROVAL"|"EXECUTING"|"COMPLETED"|"DROPPED"|"EXPIRED",
  "operator_email": "lutz@example.com",
  "ts_opened":  "2026-05-20T17:55:00Z",
  "ts_updated": "2026-05-20T18:02:00Z",
  "history": [
    {"ts": ..., "from": "operator"|"bot", "text": "..."},
    ...
  ],
  "agent_invocations": [
    {"ts": ..., "invocation_id": "cc_...", "staged_plan_path": null, "iterations": 2, "cost_usd": 0.005}
  ],
  "staged_plan_path": null | "/path/to/proposed_plan.json",
  "final_invoice_id": null | "2296"
}
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# Idle timeout: open intents past this become EXPIRED on next poll.
INTENT_IDLE_TIMEOUT_HOURS: int = 24


class IntentStatus(str, Enum):
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    DROPPED = "DROPPED"
    EXPIRED = "EXPIRED"


# Terminal states — no further routing happens.
TERMINAL_STATES = {
    IntentStatus.COMPLETED, IntentStatus.DROPPED, IntentStatus.EXPIRED,
}


def _state_root() -> Path:
    return Path(os.path.expanduser(
        "~/Library/Application Support/SAI/receipt-collector/email_intents"
    ))


def path_for(thread_id: str) -> Path:
    return _state_root() / f"{thread_id}.json"


@dataclass
class HistoryRow:
    ts: str
    sender: str         # "operator" | "bot"
    text: str


@dataclass
class AgentInvocationRow:
    ts: str
    invocation_id: str
    iterations: int
    cost_usd: float
    staged_plan_path: Optional[str] = None
    proposal_id: Optional[str] = None


# Discriminator for the kind of intent this thread is tracking.
# Existing on-disk intents lack the field — they default to
# "cost_compiler" for backward compatibility.
IntentKind = str  # "cost_compiler" | "ad_hoc"


@dataclass
class EmailIntent:
    thread_id: str
    status: IntentStatus
    operator_email: str
    ts_opened: str
    ts_updated: str
    trigger_subject: str = ""
    # Which downstream router handles replies on this thread. The
    # cost_compiler kind is the legacy default; ad_hoc covers case (c)
    # of the three-case taxonomy (TLDR + STEPS + Approve y/n).
    intent_kind: IntentKind = "cost_compiler"
    history: list[HistoryRow] = field(default_factory=list)
    agent_invocations: list[AgentInvocationRow] = field(default_factory=list)
    staged_plan_path: Optional[str] = None
    final_invoice_id: Optional[str] = None
    # Case-(c) only: the operator's original request text and the
    # bot's last STEPS proposal. Needed to replay execute_ad_hoc_steps
    # on approval without re-classifying.
    ad_hoc_original_request: Optional[str] = None
    ad_hoc_last_proposal: Optional[str] = None
    # Gmail message IDs the bot has sent on this thread. Used by the
    # poll loop to skip the bot's own replies (which otherwise look
    # like operator replies since they share the same From address as
    # the authenticated Gmail account).
    bot_sent_message_ids: list[str] = field(default_factory=list)
    # Gmail message IDs of operator messages already processed by the
    # daemon (whether handled as trigger or as a reply). Persisting
    # this in the intent state ensures the in-memory `seen_message_ids`
    # set's loss-on-restart doesn't cause re-processing.
    processed_operator_message_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "status": self.status.value,
            "operator_email": self.operator_email,
            "ts_opened": self.ts_opened,
            "ts_updated": self.ts_updated,
            "trigger_subject": self.trigger_subject,
            "intent_kind": self.intent_kind,
            "history": [asdict(h) for h in self.history],
            "agent_invocations": [asdict(a) for a in self.agent_invocations],
            "staged_plan_path": self.staged_plan_path,
            "final_invoice_id": self.final_invoice_id,
            "ad_hoc_original_request": self.ad_hoc_original_request,
            "ad_hoc_last_proposal": self.ad_hoc_last_proposal,
            "bot_sent_message_ids": list(self.bot_sent_message_ids),
            "processed_operator_message_ids": list(self.processed_operator_message_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmailIntent":
        return cls(
            thread_id=d["thread_id"],
            status=IntentStatus(d["status"]),
            operator_email=d["operator_email"],
            ts_opened=d["ts_opened"],
            ts_updated=d["ts_updated"],
            trigger_subject=d.get("trigger_subject", ""),
            intent_kind=d.get("intent_kind", "cost_compiler"),
            history=[HistoryRow(**h) for h in d.get("history", [])],
            agent_invocations=[
                AgentInvocationRow(**a) for a in d.get("agent_invocations", [])
            ],
            staged_plan_path=d.get("staged_plan_path"),
            final_invoice_id=d.get("final_invoice_id"),
            ad_hoc_original_request=d.get("ad_hoc_original_request"),
            ad_hoc_last_proposal=d.get("ad_hoc_last_proposal"),
            bot_sent_message_ids=list(d.get("bot_sent_message_ids") or []),
            processed_operator_message_ids=list(
                d.get("processed_operator_message_ids") or []
            ),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(thread_id: str) -> Optional[EmailIntent]:
    p = path_for(thread_id)
    if not p.exists():
        return None
    try:
        return EmailIntent.from_dict(json.loads(p.read_text()))
    except Exception:
        return None


def save(intent: EmailIntent) -> None:
    p = path_for(intent.thread_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    intent.ts_updated = _now_iso()
    p.write_text(json.dumps(intent.to_dict(), indent=2))


def open_intent(
    thread_id: str,
    operator_email: str,
    trigger_subject: str,
    first_text: str,
    *,
    intent_kind: IntentKind = "cost_compiler",
    initial_status: IntentStatus = IntentStatus.AWAITING_CLARIFICATION,
) -> EmailIntent:
    """Create a new intent with the operator's first message recorded.

    For the cost_compiler kind, the intent starts in
    AWAITING_CLARIFICATION (the agent may need to ask a follow-up).
    For the ad_hoc kind (case c), the caller is expected to pass
    `initial_status=AWAITING_APPROVAL` after staging the proposal,
    since the proposal IS the first bot reply and the operator's next
    turn is approve/reject."""
    now = _now_iso()
    intent = EmailIntent(
        thread_id=thread_id,
        status=initial_status,
        operator_email=operator_email,
        ts_opened=now,
        ts_updated=now,
        trigger_subject=trigger_subject,
        intent_kind=intent_kind,
        history=[HistoryRow(ts=now, sender="operator", text=first_text[:2000])],
    )
    save(intent)
    return intent


def append_operator_message(intent: EmailIntent, text: str) -> None:
    intent.history.append(HistoryRow(
        ts=_now_iso(), sender="operator", text=text[:2000],
    ))


def append_bot_message(intent: EmailIntent, text: str) -> None:
    intent.history.append(HistoryRow(
        ts=_now_iso(), sender="bot", text=text[:2000],
    ))


def record_agent_invocation(
    intent: EmailIntent,
    *,
    invocation_id: str,
    iterations: int,
    cost_usd: float,
    staged_plan_path: Optional[str] = None,
    proposal_id: Optional[str] = None,
) -> None:
    intent.agent_invocations.append(AgentInvocationRow(
        ts=_now_iso(),
        invocation_id=invocation_id,
        iterations=iterations,
        cost_usd=cost_usd,
        staged_plan_path=staged_plan_path,
        proposal_id=proposal_id,
    ))


def set_status(intent: EmailIntent, status: IntentStatus) -> None:
    intent.status = status
    save(intent)


def conversation_summary(intent: EmailIntent) -> str:
    """Build a single-string conversation summary the agent re-reads on
    each re-invocation so it has full prior context (per #16g — the
    agent doesn't re-propose a rejected shape; it remembers what it
    just heard and how the operator clarified)."""
    lines: list[str] = []
    if intent.trigger_subject:
        lines.append(f"Subject: {intent.trigger_subject}")
        lines.append("")
    for h in intent.history:
        prefix = "Operator" if h.sender == "operator" else "Bot"
        lines.append(f"[{prefix}] {h.text}")
        lines.append("")
    return "\n".join(lines).strip()


def expire_idle_intents(*, hours: int = INTENT_IDLE_TIMEOUT_HOURS) -> int:
    """Mark intents older than `hours` as EXPIRED. Returns count."""
    root = _state_root()
    if not root.exists():
        return 0
    cutoff_ts = time.time() - hours * 3600
    n = 0
    for p in root.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if d.get("status") in {s.value for s in TERMINAL_STATES}:
            continue
        try:
            updated = datetime.fromisoformat(d["ts_updated"]).timestamp()
        except Exception:
            continue
        if updated < cutoff_ts:
            d["status"] = IntentStatus.EXPIRED.value
            d["ts_updated"] = _now_iso()
            p.write_text(json.dumps(d, indent=2))
            n += 1
    return n


def open_threads() -> dict[str, IntentStatus]:
    """Return all currently-OPEN intents (status not in TERMINAL_STATES)."""
    root = _state_root()
    if not root.exists():
        return {}
    out: dict[str, IntentStatus] = {}
    for p in root.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        try:
            st = IntentStatus(d.get("status"))
        except ValueError:
            continue
        if st not in TERMINAL_STATES:
            out[d["thread_id"]] = st
    return out
