"""dispatch_agent — full-interface email dispatcher for sai@example.com.

Replaces `lib/intent_router` with a richer classifier. Every email
arriving at sai@ gets one of SIX verdicts; the daemon always sends
SOME reply unless the email is pure noise the operator didn't author.

The verdicts map onto the three-case operator taxonomy:

  CASE (a) — KNOWN WORKFLOW: SAI runs the registered workflow and
             returns its status.
    COST_COMPILER        → run cost_compiler_agent (existing flow)
    EVAL_FEEDBACK        → log to eval_feedback_inbox.jsonl + send a
                           brief confirmation reply
    GENERAL_QUERY        → invoke lib.general_assistant.respond_to_query
                           (Claude + web_search) and send the answer

  CASE (b) — NO APPROVED WORKFLOW, NO EXISTING TOOLS:
    WORKFLOW_SUGGESTION  → invoke lib.general_assistant.propose_workflow
                           which emits the three-section template
                           (headline / 100-word explanation /
                           CLAUDE CODE PROMPT). Per SAI #9 the email
                           channel NEVER creates or edits a workflow.

  CASE (c) — NO APPROVED WORKFLOW, BUT SAI HAS THE TOOLS:
    AD_HOC_CAPABLE       → invoke lib.general_assistant.propose_ad_hoc_steps
                           which emits the TLDR + STEPS + Approve y/n
                           template. On the operator's next turn, if
                           they reply 'y', email_runner re-routes
                           through the AD_HOC executor (read-only
                           tools only).

  IGNORE                 → pure noise (forwarded auto-mail the operator
                           didn't annotate). Mark read; no reply.

Per SAI principle #12 (cascade with early-stop): rules tier first
(zero LLM cost on obvious cases), Haiku tier when rules abstain.

Per SAI #6a (output guard) the LLM call uses a strict JSON Schema
with `enum` on the verdict so we never silently coerce a typo into
a real verdict — unknown verdicts fail closed to IGNORE.

The dispatcher does NOT execute downstream work itself — it returns a
`Dispatch` decision; `email_runner` does the routing.

The classifier output is logged to ~/Library/Logs/SAI/dispatch_agent.jsonl
for audit.
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


AUDIT_PATH = Path.home() / "Library" / "Logs" / "SAI" / "dispatch_agent.jsonl"
EVAL_INBOX_PATH = Path.home() / "Library" / "Logs" / "SAI" / "eval_feedback_inbox.jsonl"


class Verdict(str, Enum):
    COST_COMPILER = "COST_COMPILER"
    EVAL_FEEDBACK = "EVAL_FEEDBACK"
    GENERAL_QUERY = "GENERAL_QUERY"
    AD_HOC_CAPABLE = "AD_HOC_CAPABLE"
    WORKFLOW_SUGGESTION = "WORKFLOW_SUGGESTION"
    IGNORE = "IGNORE"


# Which verdicts map to which operator-facing case in the three-case
# taxonomy. Used by audit + tests; the routing in email_runner.py
# branches on Verdict, not on this mapping.
CASE_FOR_VERDICT: dict[Verdict, str] = {
    Verdict.COST_COMPILER: "a",
    Verdict.EVAL_FEEDBACK: "a",
    Verdict.GENERAL_QUERY: "a",
    Verdict.AD_HOC_CAPABLE: "c",
    Verdict.WORKFLOW_SUGGESTION: "b",
    Verdict.IGNORE: "ignore",
}


@dataclass
class Dispatch:
    verdict: Verdict
    confidence: str        # "high" | "medium" | "low"
    source: str            # "rule:<name>" | "llm:<model>" | "default"
    reason: str
    cost_usd: float = 0.0


# ─── rules tier ────────────────────────────────────────────────────────
#
# We keep ALL rules deliberately conservative — they fire ONLY on
# unambiguous matches. Everything else escalates to the LLM tier.

# Obvious IGNORE patterns: forwarded auto-mail without operator text.
# (The Haiku tier separately decides if the operator ADDED text to a
# forward, which would route to GENERAL_QUERY instead of IGNORE.)
_IGNORE_PATTERNS = [
    (r"\bboarding\s+(for|has\s+begun|is\s+now)\b", "boarding_pass"),
    (r"\bflight\s+is\s+now\s+boarding\b", "boarding_pass"),
    (r"\bgate\s+(change|opens|closing)\b", "live_flight_notice"),
    (r"\b(a\s+)?shipment\s+from\s+order\b", "shipment_notice"),
    (r"\border\s+#\w+\s+has\s+been\s+(delivered|shipped)\b", "shipment_notice"),
]

# Explicit cost-compilation triggers
_COST_COMPILER_PATTERNS = [
    (r"\bcost[\s-]*compil(er|ation|e)\b", "explicit_cost_compile"),
    (r"\bcompile\s+(my\s+)?(travel\s+)?(receipts|costs|expenses)\b", "compile_costs"),
    (r"\bfile\s+(my\s+)?(travel\s+)?(receipts|expenses)\b", "file_receipts"),
    (r"\bbill\s+(my\s+)?(trip|customer)\b", "bill_trip"),
    (r"\bcreate\s+(an?\s+)?invoice\s+for\b.*\b(trip|customer)\b", "create_invoice"),
    (r"\b(process|reconcile|stage)\s+(my\s+)?(travel|trip)\s+(expenses|receipts|costs)\b", "process_trip"),
    (r"\bprepare\s+(an?\s+)?(invoice|expense\s+report)\b.*\b(trip|customer)\b", "prepare_invoice"),
]

# Explicit eval-feedback signals
_EVAL_FEEDBACK_PATTERNS = [
    (r"\bwrong\s+label\b", "eval_wrong_label"),
    (r"\bright\s+label\s+is\b", "eval_right_label_is"),
    (r"\bshould\s+have\s+been\s+(L[0-9]/|tagged|labeled|filed)\b", "eval_should_have_been"),
    (r"\b(re-?label|re-?classify|reclassify)\b", "eval_relabel"),
]


def _rules_classify(subject: str, body: str) -> Optional[Dispatch]:
    """Try to classify deterministically. Returns None on ambiguity."""
    text = f"{subject}\n{body}".lower()
    for pat, name in _COST_COMPILER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return Dispatch(Verdict.COST_COMPILER, "high",
                            f"rule:{name}", f"matched {pat!r}")
    for pat, name in _EVAL_FEEDBACK_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return Dispatch(Verdict.EVAL_FEEDBACK, "high",
                            f"rule:{name}", f"matched {pat!r}")
    for pat, name in _IGNORE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return Dispatch(Verdict.IGNORE, "high",
                            f"rule:{name}", f"matched {pat!r}")
    return None


# ─── LLM tier ───────────────────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = """\
You're the front-line dispatcher for an operator's `sai@` inbox.
Classify each incoming email into ONE of SIX verdicts and reply
with strict JSON.

The verdict you pick determines which of three operator-facing reply
shapes the daemon will send:
  CASE (a) — known workflow → just execute + return status
  CASE (b) — unknown workflow AND SAI has no existing tools to do it
             → reply explains what would have to be built, ends with a
                copy-paste prompt for Claude Code
  CASE (c) — unknown workflow BUT SAI has the read-only tools to do it
             → reply offers TLDR + concrete STEPS + "Approve y/n"

Verdict catalog:

  COST_COMPILER   (case a)
      Operator wants you to compile travel costs into a QuickBooks
      invoice for a specific customer trip. Triggers usually mention:
      a customer name, dates or a month, the word "trip"/"receipts"/
      "expenses", and intent to bill someone.

  EVAL_FEEDBACK   (case a)
      Operator is correcting an AI-applied label. Triggers usually
      contain "wrong label", "right label is", "should be L1/X",
      "reclassify", or a quoted prior auto-classification.

  GENERAL_QUERY   (case a — the "known workflow" is "answer with Claude")
      Operator is asking a normal question — research ("what's the
      latest on X?"), opinion ("what do you think of Y?"), code
      help, jokes, brainstorming, factual lookup. Anything they'd
      have asked a chat assistant. The bot will reply with a useful
      answer (possibly using web search). NO side effects beyond
      the answer itself.

  AD_HOC_CAPABLE   (case c)
      Operator wants something specific done that does not match a
      registered workflow BUT can be done with SAI's existing
      read-only tools (Gmail search, Drive search if connected,
      Granola search if connected, web search, reasoning) — and the
      task would naturally span multiple steps the operator would
      want to see before SAI runs them. Examples:
        - "have I ever signed a partner agreement for <Vendor>"
        - "find every email where <person> mentioned <topic>"
        - "summarise everything I've discussed with <person> in
           Granola in the last 6 months"
      The reply will list the STEPS and ask for explicit y/n
      approval before any tool runs.

  WORKFLOW_SUGGESTION   (case b)
      Operator is describing a workflow they wish existed but doesn't
      yet, AND that would need NEW code/connectors/scheduling to
      build (i.e. SAI does NOT already have the tools). Examples:
        - "could we build a thing that watches my calendar and
           proposes content every Monday"
        - "I want a workflow that auto-renames Drive files based on
           their contents"
        - "make a daily digest that pulls X, Y, Z and posts to Slack"
      The reply will articulate what the workflow would look like
      AND will end with a copy-paste prompt the operator can run in
      Claude Code. Per SAI #9, the email channel itself NEVER edits
      code, prompts, or policy.

  IGNORE
      Forwarded auto-mail the operator did NOT annotate (boarding
      passes, shipment notifications, alumni invites, vendor
      newsletters they're just filing). No reply needed.

      IMPORTANT: if the operator added their own text on top of a
      forward — even one sentence asking about the content — that
      is GENERAL_QUERY (case a) or AD_HOC_CAPABLE (case c) depending
      on whether they're asking a question vs. requesting a search.

DECISION RULES (apply in order):
  1. IRREVERSIBLE side effect required? If the operator's outcome can
     only be delivered by SAI actually SENDING an email, BOOKING a
     meeting, POSTING somewhere, MUTATING Gmail labels, or EDITING
     code/prompts/policy → case (b) WORKFLOW_SUGGESTION.
     CRITICAL EXCEPTION — REVERSIBLE writes are NOT irreversible side
     effects, so they do NOT trigger this rule. Two reversible writes
     SAI may stage directly (operator reviews/deletes afterward):
       * a Gmail DRAFT (sits in Drafts, never auto-sent), and
       * a CALENDAR EVENT / time-block (operator edits or deletes it).
     So "draft a reply to <person> about <topic>" AND "book/block
     travel time to <event>" both decompose into read-only context-
     gathering + a single reversible write → AD_HOC_CAPABLE (case c),
     NOT WORKFLOW_SUGGESTION. Use case (b) only when the USEFUL outcome
     IS an IRREVERSIBLE action: SENDING the email now, paying, posting
     publicly, deleting data, or editing code/prompts/policy.
  2. Otherwise, is the work doable with the read-only tool surface
     above, OR is it a reversible-write task — "draft a reply / draft
     an email" (→ Gmail draft) or "book/block travel time / add a
     calendar block" (→ calendar event)? Yes → AD_HOC_CAPABLE.
  3. Otherwise, does it look like a registered workflow's trigger?
     Yes → that workflow's verdict.
  4. Otherwise → IGNORE (pure noise) only if the operator did NOT
     write any of the body themselves. If they wrote even one line,
     default to GENERAL_QUERY.

Return ONE JSON object exactly:
  {"verdict": "COST_COMPILER"|"EVAL_FEEDBACK"|"GENERAL_QUERY"|"AD_HOC_CAPABLE"|"WORKFLOW_SUGGESTION"|"IGNORE",
   "confidence": "high"|"medium"|"low",
   "reason": "one short sentence — why this verdict"}

No prose around the JSON. No code fences."""


def _llm_classify(subject: str, body: str, overlay: dict) -> Dispatch:
    """Haiku-tier classification when rules abstain."""
    try:
        import anthropic
    except ImportError:
        return Dispatch(Verdict.IGNORE, "low", "default",
                        "anthropic SDK missing — fail closed to IGNORE")
    from lib import op_env, llm_costs

    try:
        llm_costs.enforce_daily_cap(
            skill="receipt-collector", step="dispatch_agent",
            upcoming_usd_cost=0.001, overlay=overlay,
        )
    except llm_costs.BudgetExceeded as bx:
        return Dispatch(Verdict.IGNORE, "low", "default",
                        f"budget cap hit; fail closed: {bx}")

    api_key = op_env.get_cached_secret("anthropic")
    if not api_key:
        return Dispatch(Verdict.IGNORE, "low", "default",
                        "no Anthropic key in Keychain")

    user_msg = (
        f"Subject: {subject[:200]}\n\n"
        f"Body (first 1500 chars):\n{body[:1500]}"
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=_LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return Dispatch(Verdict.IGNORE, "low", "default",
                        f"LLM call failed: {e}")

    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if not m:
            return Dispatch(Verdict.IGNORE, "low", "default",
                            f"LLM non-JSON: {raw[:120]}")
        try:
            data = json.loads(m.group(0))
        except Exception:
            return Dispatch(Verdict.IGNORE, "low", "default",
                            f"LLM JSON malformed: {raw[:120]}")

    v_raw = (data.get("verdict") or "").upper().strip()
    if v_raw not in {v.value for v in Verdict}:
        return Dispatch(Verdict.IGNORE, "low", "default",
                        f"LLM unknown verdict: {v_raw!r}")

    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    cost = (in_tok * 1.0 + out_tok * 5.0) / 1_000_000
    llm_costs.log_call(
        skill="receipt-collector", step="dispatch_agent",
        model="claude-haiku-4-5",
        input_tokens=in_tok, output_tokens=out_tok,
        usd_cost=cost, note=f"verdict={v_raw}",
    )
    return Dispatch(
        verdict=Verdict(v_raw),
        confidence=(data.get("confidence") or "medium"),
        source=f"llm:claude-haiku-4-5",
        reason=(data.get("reason") or "")[:200],
        cost_usd=cost,
    )


def classify(subject: str, body: str, overlay: dict) -> Dispatch:
    """Two-tier classification."""
    rules_verdict = _rules_classify(subject, body)
    if rules_verdict is not None:
        return rules_verdict
    return _llm_classify(subject, body, overlay)


def log_dispatch(thread_id: str, msg_id: str, subject: str,
                 dispatch: Dispatch) -> None:
    """Append one JSONL row per dispatch decision."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "thread_id": thread_id,
        "msg_id": msg_id,
        "subject": subject[:200],
        "verdict": dispatch.verdict.value,
        "confidence": dispatch.confidence,
        "source": dispatch.source,
        "reason": dispatch.reason,
        "cost_usd": round(dispatch.cost_usd, 6),
    }
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def log_eval_feedback(thread_id: str, msg_id: str, subject: str,
                      body: str) -> None:
    """When verdict=EVAL_FEEDBACK, drop the email into a JSONL inbox
    the (future/separate) sai-eval workflow can read."""
    EVAL_INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "thread_id": thread_id,
        "msg_id": msg_id,
        "subject": subject[:200],
        "body": body[:4000],
        "routed_by": "cost-compiler/dispatch_agent",
    }
    with EVAL_INBOX_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")
