"""intent_router — classify incoming sai@ emails before opening an intent.

The operator's `sai@lutzfinger.com` inbox is shared by multiple SAI
workflows:
  * cost-compiler (this skill) — receipt collection for customer trips
  * eval feedback — operator notes corrections to AI-applied labels
  * email-to-calendar — forwarded events to be added to calendar
  * pure noise — boarding passes, alumni invites, vendor newsletters
    the operator forwards for their own filing

If the cost-compiler daemon opens an intent for EVERY new email,
it spams the operator with "is this for me?" replies (which is
exactly what happened in the 2026-05-20 self-test — 8 unwanted
clarification emails went out for forwarded boarding passes).

This router runs FIRST, per SAI #12 cascade (rules-tier deterministic
first, LLM-tier fallback):

  rules:  cheap regex match on subject + body
  llm:    Claude Haiku one-shot classification when rules abstain

Output:
  Verdict.COST_COMPILER  → open intent + invoke cost_compiler_agent
  Verdict.EVAL_FEEDBACK  → log to ~/Library/Logs/SAI/eval_feedback_inbox.jsonl,
                            mark email as read, NO reply
  Verdict.OTHER          → mark email as read, NO reply, just log

Per SAI #6 fail-closed: if rules abstain AND the LLM is unavailable
(daily cap, no key), the verdict is OTHER. We'd rather drop a real
trigger silently than spam clarifications on noise. Operator can
re-send a clearer trigger.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


EVAL_INBOX_PATH = Path.home() / "Library" / "Logs" / "SAI" / "eval_feedback_inbox.jsonl"
ROUTER_AUDIT_PATH = Path.home() / "Library" / "Logs" / "SAI" / "intent_router.jsonl"


class Verdict(str, Enum):
    COST_COMPILER = "COST_COMPILER"
    EVAL_FEEDBACK = "EVAL_FEEDBACK"
    OTHER = "OTHER"


@dataclass
class Routing:
    verdict: Verdict
    confidence: str            # "high" | "medium" | "low"
    source: str                # "rule:<name>" | "llm:<model>" | "default"
    reason: str
    cost_usd: float = 0.0


# ─── rules tier ─────────────────────────────────────────────────────────
#
# Each rule = a regex against the combined subject+body text. The
# patterns are deliberately conservative — they catch CLEARLY one or
# the other; anything ambiguous falls through to the LLM.

_NOT_COST_COMPILER_PATTERNS = [
    # boarding passes / live travel notifications (the actual cause of
    # the 8-email-spam incident in the 2026-05-20 self-test)
    (r"\bboarding\s+(for|has\s+begun|is\s+now)", "boarding_pass"),
    (r"\bflight\s+is\s+now\s+boarding", "boarding_pass"),
    (r"\bgate\s+(change|opens|closing)", "live_flight_notice"),
    (r"\b(your|the)\s+flight\s+(has\s+been\s+delayed|is\s+delayed)", "delay_notice"),
    # shipment / order
    (r"\b(a\s+)?shipment\s+from\s+order\b", "shipment_notice"),
    (r"\border\s+#\w+\s+has\s+been\s+(delivered|shipped)", "shipment_notice"),
    # community / alumni
    (r"\balumni\b.*\b(asked\s+to\s+join|invite|membership)", "alumni_invite"),
    (r"\binvitation\s+to\s+join\b", "community_invite"),
    # availability / scheduling / expert intros (forwarded vendor mail)
    (r"\bEXPERT\s+AVAIL", "vendor_intro"),
    (r"\bchecking\s+in\s+from\b", "vendor_followup"),
    # newsletters / digests
    (r"\b(newsletter|digest|weekly\s+round-?up)\b", "newsletter"),
    # eval-feedback signals (the operator correcting an AI label)
    (r"\bwrong\s+label\b", "eval_feedback_label"),
    (r"\bright\s+label\s+is\b", "eval_feedback_label"),
    (r"\bshould\s+have\s+been\s+(L1|L2|tagged|labeled|filed)\b", "eval_feedback_correction"),
    (r"\b(re-?label|re-?classify|reclassify)\b", "eval_feedback_correction"),
]

_LIKELY_COST_COMPILER_PATTERNS = [
    # explicit invocation
    (r"\bcost[\s-]*compil(er|ation|e)\b", "explicit_cost_compile"),
    (r"\bcompile\s+(my\s+)?(travel\s+)?(receipts|costs|expenses)\b", "compile_costs"),
    (r"\bfile\s+(my\s+)?(travel\s+)?(receipts|expenses)\b", "file_receipts"),
    (r"\bbill\s+(my\s+)?(trip|customer)\b", "bill_trip"),
    (r"\bcreate\s+(an?\s+)?invoice\s+for\b.*\b(trip|customer)", "create_invoice"),
    # natural-language "process my trip"
    (r"\b(process|reconcile|stage)\s+(my\s+)?(travel|trip)\s+(expenses|receipts|costs)", "process_trip"),
    (r"\b(prepare|build)\s+(an?\s+)?(invoice|expense\s+report)\b.*\b(trip|customer)", "prepare_invoice"),
]


def _rules_classify(subject: str, body: str) -> Optional[Routing]:
    """Try to classify deterministically. Returns None if ambiguous —
    caller escalates to LLM tier."""
    text = f"{subject}\n{body}".lower()
    for pat, name in _NOT_COST_COMPILER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            verdict = (
                Verdict.EVAL_FEEDBACK
                if name.startswith("eval_feedback")
                else Verdict.OTHER
            )
            return Routing(
                verdict=verdict, confidence="high",
                source=f"rule:{name}",
                reason=f"matched pattern {pat!r}",
            )
    for pat, name in _LIKELY_COST_COMPILER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return Routing(
                verdict=Verdict.COST_COMPILER, confidence="high",
                source=f"rule:{name}",
                reason=f"matched pattern {pat!r}",
            )
    return None


# ─── LLM tier ───────────────────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = """\
You are the email router for a single operator's `sai@` inbox. Each
incoming email belongs to ONE of three workflows:

  COST_COMPILER  — operator wants you to compile travel costs for a
                   business trip into a QuickBooks invoice. Triggers
                   usually mention: a customer name, a date window or
                   month, the word "trip" or "receipts" or "expenses",
                   and an intent to bill someone.

  EVAL_FEEDBACK  — operator is correcting an AI-applied label or
                   classification. Triggers usually contain "wrong
                   label", "should be", "right label is", or quote a
                   prior auto-classified email and explain why the
                   classification was wrong.

  OTHER          — everything else: forwarded boarding passes,
                   delivery notifications, alumni invites, vendor
                   intros, generic newsletters, personal mail, etc.
                   These should be IGNORED (not replied to).

Return ONE JSON object exactly:
  {"verdict": "COST_COMPILER"|"EVAL_FEEDBACK"|"OTHER",
   "confidence": "high"|"medium"|"low",
   "reason": "one short sentence"}

No prose around the JSON. No code fences."""


def _llm_classify(subject: str, body: str, overlay: dict) -> Routing:
    """LLM tier: ask Haiku to classify. Stays under daily cap."""
    # Lazy imports so the module loads even if anthropic SDK missing.
    try:
        import anthropic
    except ImportError:
        return Routing(
            verdict=Verdict.OTHER, confidence="low",
            source="default", reason="anthropic SDK missing — fail closed to OTHER",
        )
    from lib import op_env, llm_costs

    # Honor daily cap.
    try:
        llm_costs.enforce_daily_cap(
            skill="receipt-collector",
            step="intent_router",
            upcoming_usd_cost=0.001,
            overlay=overlay,
        )
    except Exception as e:
        return Routing(
            verdict=Verdict.OTHER, confidence="low",
            source="default", reason=f"budget cap hit; fail closed: {e}",
        )

    # Get API key (cached in Keychain, no `op` prompt).
    cached = op_env.get_cached_secret("anthropic")
    if not cached:
        return Routing(
            verdict=Verdict.OTHER, confidence="low",
            source="default",
            reason="no Anthropic key in Keychain; fail closed",
        )

    user_msg = (
        f"Subject: {subject[:200]}\n\n"
        f"Body (first 1200 chars):\n{body[:1200]}"
    )
    try:
        client = anthropic.Anthropic(api_key=cached)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=_LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return Routing(
            verdict=Verdict.OTHER, confidence="low",
            source="default", reason=f"LLM call failed: {e}",
        )

    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if not m:
            return Routing(
                verdict=Verdict.OTHER, confidence="low",
                source="default", reason=f"LLM non-JSON: {raw[:120]}",
            )
        try:
            data = json.loads(m.group(0))
        except Exception:
            return Routing(
                verdict=Verdict.OTHER, confidence="low",
                source="default", reason=f"LLM JSON malformed: {raw[:120]}",
            )

    v_raw = (data.get("verdict") or "").upper().strip()
    if v_raw not in {v.value for v in Verdict}:
        return Routing(
            verdict=Verdict.OTHER, confidence="low",
            source="default",
            reason=f"LLM unknown verdict: {v_raw!r}",
        )

    # Track cost.
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    cost = (in_tok * 1.0 + out_tok * 5.0) / 1_000_000
    llm_costs.log_call(
        skill="receipt-collector", step="intent_router",
        model="claude-haiku-4-5",
        input_tokens=in_tok, output_tokens=out_tok,
        usd_cost=cost, note=f"verdict={v_raw}",
    )
    return Routing(
        verdict=Verdict(v_raw),
        confidence=(data.get("confidence") or "medium"),
        source=f"llm:claude-haiku-4-5",
        reason=(data.get("reason") or "")[:200],
        cost_usd=cost,
    )


# ─── public entrypoint ──────────────────────────────────────────────────

def classify(subject: str, body: str, overlay: dict) -> Routing:
    """Two-tier classification.

    Rules tier handles the obvious cases (boarding passes, explicit
    cost-compiler asks, eval-feedback signals). LLM tier handles
    ambiguous text.

    Per SAI #6 fail-closed: when LLM is unavailable (no key, cap hit,
    network error), default to OTHER. Better to drop a real trigger
    than spam clarifications on noise; operator can re-send.
    """
    rules_verdict = _rules_classify(subject, body)
    if rules_verdict is not None:
        return rules_verdict
    return _llm_classify(subject, body, overlay)


def log_routing(thread_id: str, msg_id: str, subject: str,
                routing: Routing) -> None:
    """Append one JSONL row per routing decision for audit."""
    ROUTER_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "thread_id": thread_id,
        "msg_id": msg_id,
        "subject": subject[:200],
        "verdict": routing.verdict.value,
        "confidence": routing.confidence,
        "source": routing.source,
        "reason": routing.reason,
        "cost_usd": round(routing.cost_usd, 6),
    }
    with ROUTER_AUDIT_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def log_eval_feedback(thread_id: str, msg_id: str, subject: str,
                      body: str) -> None:
    """When verdict=EVAL_FEEDBACK, drop the email into a JSONL inbox
    the (future) sai-eval workflow can read. The cost-compiler skill
    doesn't try to handle eval feedback itself — per SAI #33a (Skills
    compose, framework primitives are separate work)."""
    EVAL_INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "thread_id": thread_id,
        "msg_id": msg_id,
        "subject": subject[:200],
        "body": body[:4000],
        "routed_by": "cost-compiler/intent_router",
    }
    with EVAL_INBOX_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")
