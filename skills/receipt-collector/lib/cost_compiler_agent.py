"""cost-compiler trigger agent — Anthropic tool-use edition.

Architecture mirrors the slack-eval-agent pattern (see
`app/agents/sai_eval_agent.py` in SAI baseversion), with two
deliberate simplifications:

  * Direct Anthropic SDK rather than LangChain. The slack-eval agent
    uses LangChain because it's part of the SAI v8 cascade framework;
    a skill-local agent is cleaner with the Anthropic `tool_use` API
    directly. Future framework promotion can wrap this in LangChain.
  * Skill-local prompt file (not hash-locked via `prompts/prompt-locks.yaml`).
    A future Phase E task migrates this to the hash-verifying loader
    per principle #24c.

Cascade (per SAI principle #12):

    operator trigger
      │
      ├─[llm_agent — this module]──MATCH───→ propose_plan staged
      │                                      operator approves via
      │                                      await-approval
      │
      └─[rules_fallback]──── LLM unreachable ──→ parse_trigger.parse
                                                  (deterministic; legacy)

Supervisory layer:
  * MAX_ITERATIONS = 6 (hard cap on agent steps per invocation)
  * Daily-cap honored via lib.llm_costs.enforce_daily_cap BEFORE
    every Anthropic call (per #28 hard ceilings)
  * Per-invocation audit row written to
    ~/Library/Logs/SAI/cost_compiler_agent.jsonl
  * Per-LLM-call cost row written to
    ~/Library/Logs/SAI/llm_costs.jsonl
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib import cost_compiler_tools as tools
from lib import llm_costs
from lib import parse_trigger
from lib.qb_client import QBClient


MAX_ITERATIONS: int = 6
"""Hard cap on agent steps per invocation. Each iteration = one
LLM call. The slack-eval agent uses 8; cost-compiler trigger
interpretation needs fewer steps (typically 2-3: list_qb_customers
→ propose_plan, sometimes + search_calendar_events)."""

DEFAULT_MODEL: str = "claude-haiku-4-5"
"""Cheapest paid vision-capable Claude tier. Slack-eval uses
`claude-haiku-4-5-20251001` (a dated version); for the skill-local
agent we use the floating alias so model upgrades happen via
config, not code edits (per #24b)."""

PROMPT_RELPATH: str = "prompts/cost_compiler_agent.md"
"""Resolved relative to the skill root."""

AUDIT_PATH: Path = (
    Path.home() / "Library" / "Logs" / "SAI" / "cost_compiler_agent.jsonl"
)

# Haiku 4.5 list prices ($ per 1M tokens). Updated when Anthropic
# changes the price; the cost-tracker writes USD per call so the
# operator's daily cap is enforced correctly.
HAIKU_INPUT_PRICE_PER_MTOK: float = 1.00
HAIKU_OUTPUT_PRICE_PER_MTOK: float = 5.00


@dataclass
class AgentInvocation:
    invocation_id: str
    started_at: str
    source_text: str
    model_used: str
    iterations: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    final_text: str = ""
    staged_plan_path: Optional[str] = None
    proposal_id: Optional[str] = None
    cost_usd: float = 0.0
    terminated_reason: str = "end_turn"  # end_turn | proposed | iteration_cap | error | budget_exceeded
    error: Optional[str] = None


@dataclass
class AgentResult:
    """What the surface runners (Slack/email/CLI) consume."""
    operator_message: str
    staged_plan_path: Optional[str] = None
    proposed_plan: Optional[dict] = None    # parsed contents of the staged JSON
    invocation: Optional[AgentInvocation] = None


def run_agent(
    *,
    source_text: str,
    overlay: dict,
    qb_client: Optional[QBClient] = None,
    model: Optional[str] = None,
    audit_path: Optional[Path] = None,
) -> AgentResult:
    """Run one agent turn over `source_text`. Returns AgentResult.

    The runner handles every failure mode (no API key, daily-cap hit,
    network down, iteration cap, malformed model output) by returning
    an AgentResult with the operator_message + a terminated_reason in
    the invocation row — no exceptions escape to the caller.
    """
    audit_path = audit_path or AUDIT_PATH
    invocation = AgentInvocation(
        invocation_id=_invocation_id(),
        started_at=datetime.now(timezone.utc).isoformat(),
        source_text=source_text[:500],
        model_used=model or DEFAULT_MODEL,
    )

    # ── client setup (fail closed if creds missing) ─────────────────
    try:
        api_key = _get_anthropic_key(overlay)
    except Exception as e:
        invocation.terminated_reason = "error"
        invocation.error = f"no Anthropic key: {e}"
        _write_audit(audit_path, invocation)
        return _fallback_to_rules(source_text, overlay, invocation, audit_path,
            reason=f"Anthropic key unavailable: {e}")

    try:
        import anthropic
    except ImportError as e:
        invocation.terminated_reason = "error"
        invocation.error = f"anthropic SDK missing: {e}"
        _write_audit(audit_path, invocation)
        return _fallback_to_rules(source_text, overlay, invocation, audit_path,
            reason=f"anthropic SDK not installed: {e}")

    # ── daily cap check before the FIRST call ───────────────────────
    try:
        # Estimate per call: ~3k input + ~500 output = ~$0.0055 per
        # iteration. Six iterations max ≈ $0.04 worst case. Round up
        # to $0.05 for safety.
        llm_costs.enforce_daily_cap(
            skill="receipt-collector",
            step="cost_compiler_agent",
            upcoming_usd_cost=0.05,
            overlay=overlay,
        )
    except llm_costs.BudgetExceeded as bx:
        invocation.terminated_reason = "budget_exceeded"
        invocation.error = str(bx)
        _write_audit(audit_path, invocation)
        # Fall through to rules tier rather than refusing outright —
        # the operator might still get a useful plan from the regex
        # fallback even when the daily cap is hit.
        return _fallback_to_rules(source_text, overlay, invocation, audit_path,
            reason=f"daily LLM cap reached ({bx})")

    # ── load system prompt ───────────────────────────────────────────
    skill_root = Path(__file__).resolve().parent.parent
    prompt_path = skill_root / PROMPT_RELPATH
    if not prompt_path.exists():
        invocation.terminated_reason = "error"
        invocation.error = f"prompt not found at {prompt_path}"
        _write_audit(audit_path, invocation)
        return _fallback_to_rules(source_text, overlay, invocation, audit_path,
            reason=f"system prompt missing: {prompt_path}")
    system_prompt = prompt_path.read_text()

    # ── QB client (for tool context) ────────────────────────────────
    if qb_client is None:
        from runner import qb_client_from_overlay  # type: ignore
        qb_client = qb_client_from_overlay(overlay)

    ctx = tools.ToolContext(
        overlay=overlay,
        qb_client=qb_client,
        operator_text=source_text,
    )
    dispatch = tools.build_tool_dispatch(ctx)
    client = anthropic.Anthropic(api_key=api_key)

    # ── tool-use loop ────────────────────────────────────────────────
    chosen_model = model or os.environ.get("SAI_COST_COMPILER_AGENT_MODEL") or DEFAULT_MODEL
    messages: list[dict] = [
        {"role": "user", "content": source_text}
    ]
    iteration = 0
    final_text = ""
    while iteration < MAX_ITERATIONS:
        iteration += 1
        # Re-check daily cap before each iteration (cumulative usage
        # this run already counted; this catches a cap-breaking call
        # mid-loop).
        try:
            llm_costs.enforce_daily_cap(
                skill="receipt-collector",
                step="cost_compiler_agent",
                upcoming_usd_cost=0.01,
                overlay=overlay,
            )
        except llm_costs.BudgetExceeded as bx:
            invocation.terminated_reason = "budget_exceeded"
            invocation.error = f"mid-loop: {bx}"
            break

        try:
            resp = client.messages.create(
                model=chosen_model,
                max_tokens=1024,
                system=system_prompt,
                tools=tools.ANTHROPIC_TOOL_SPECS,
                messages=messages,
            )
        except Exception as e:
            invocation.terminated_reason = "error"
            invocation.error = f"anthropic call failed: {e}"
            break

        # Track cost.
        in_tok = getattr(resp.usage, "input_tokens", 0) or 0
        out_tok = getattr(resp.usage, "output_tokens", 0) or 0
        call_cost = (in_tok * HAIKU_INPUT_PRICE_PER_MTOK
                     + out_tok * HAIKU_OUTPUT_PRICE_PER_MTOK) / 1_000_000
        invocation.cost_usd += call_cost
        llm_costs.log_call(
            skill="receipt-collector",
            step="cost_compiler_agent",
            model=chosen_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            usd_cost=call_cost,
            note=f"iter={iteration} stop_reason={resp.stop_reason}",
        )

        # Collect any text the model emitted this turn.
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                final_text = block.text

        # If the model used a tool, dispatch + append the result and loop.
        tool_uses = [b for b in (resp.content or [])
                     if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            # No more tool calls — the model said its piece.
            invocation.terminated_reason = "end_turn"
            break

        # Append the assistant's tool_use block, then the tool results,
        # then continue the loop.
        messages.append({"role": "assistant", "content": resp.content})
        tool_results: list[dict] = []
        for tu in tool_uses:
            name = tu.name
            tu_id = tu.id
            tu_input = tu.input or {}
            fn = dispatch.get(name)
            if fn is None:
                result_dict = {"error": f"unknown tool {name!r}"}
            else:
                try:
                    result_dict = fn(**tu_input)
                except TypeError as e:
                    result_dict = {"error": f"bad arguments to {name}: {e}"}
                except Exception as e:
                    result_dict = {"error": f"{type(e).__name__}: {e}"}
            invocation.tool_calls.append({
                "iter": iteration,
                "tool": name,
                "input": tu_input,
                "result_truncated": json.dumps(result_dict)[:1500],
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": json.dumps(result_dict),
            })
            # If propose_plan succeeded, capture the staged path now;
            # we'll surface it in AgentResult after the loop exits.
            if name == "propose_plan" and "staged_path" in result_dict:
                invocation.staged_plan_path = result_dict["staged_path"]
                invocation.proposal_id = result_dict.get("proposal_id")
        messages.append({"role": "user", "content": tool_results})

    else:
        # Loop exhausted MAX_ITERATIONS without `break`
        invocation.terminated_reason = "iteration_cap"

    invocation.iterations = iteration
    invocation.final_text = final_text
    _write_audit(audit_path, invocation)

    # ── package result ───────────────────────────────────────────────
    proposed_plan = None
    if invocation.staged_plan_path:
        try:
            proposed_plan = json.loads(
                Path(invocation.staged_plan_path).read_text()
            )
        except Exception:
            proposed_plan = None

    # Operator-facing message: if propose_plan ran, the tool's
    # operator_message is the canonical text. Otherwise return the
    # model's final assistant text (which should be a clarification).
    operator_message = final_text or _generic_fallback_message()
    for tc in invocation.tool_calls:
        if tc["tool"] == "propose_plan":
            try:
                result_dict = json.loads(tc["result_truncated"])
                if "operator_message" in result_dict:
                    operator_message = result_dict["operator_message"]
            except Exception:
                pass

    return AgentResult(
        operator_message=operator_message,
        staged_plan_path=invocation.staged_plan_path,
        proposed_plan=proposed_plan,
        invocation=invocation,
    )


# ─── helpers ──────────────────────────────────────────────────────────

def _invocation_id() -> str:
    return (
        f"cc_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{secrets.token_hex(3)}"
    )


def _get_anthropic_key(overlay: dict) -> str:
    """Read the Anthropic API key.

    Resolution order (cheapest first, no GUI prompt anywhere):
      1. ANTHROPIC_API_KEY env var (handy for tests / CI)
      2. Cached value in macOS Keychain (`sai-secret-anthropic`),
         populated by `runner cache-secrets` while the operator is at
         the keyboard. Read via `security` — does NOT trigger the
         macOS "op would like to access data" TCC prompt.
      3. If cache empty: instructive error pointing the operator at
         `cache-secrets`. We do NOT fall through to `op` here — that
         would prompt the user with a permission dialog every time
         the daemon polls, which is unacceptable per SAI #7a.

    The one-shot `cache-secrets` subcommand IS allowed to call `op`
    (any prompt then is fine — operator is at the keyboard).
    """
    # Env var override
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]

    # macOS Keychain — no `op` invocation, no TCC prompt
    from lib import op_env
    cached = op_env.get_cached_secret("anthropic")
    if cached:
        # Set in env so subsequent calls in this process don't even
        # re-hit Keychain.
        os.environ["ANTHROPIC_API_KEY"] = cached
        return cached

    raise RuntimeError(
        "Anthropic API key not cached in macOS Keychain.\n"
        "Run once at the keyboard:\n"
        "  python -m skills.receipt-collector.runner cache-secrets\n"
        "That invokes `op` exactly once to fetch the key from 1Password "
        "and stash it in Keychain so the daemon can read it later via "
        "`security` without any GUI prompt."
    )


def _write_audit(audit_path: Path, invocation: AgentInvocation) -> None:
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "invocation_id": invocation.invocation_id,
            "started_at": invocation.started_at,
            "source_text": invocation.source_text,
            "model_used": invocation.model_used,
            "iterations": invocation.iterations,
            "tool_calls": invocation.tool_calls,
            "final_text": invocation.final_text[:1000],
            "staged_plan_path": invocation.staged_plan_path,
            "proposal_id": invocation.proposal_id,
            "cost_usd": round(invocation.cost_usd, 6),
            "terminated_reason": invocation.terminated_reason,
            "error": invocation.error,
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        # Never let an audit-write failure crash the agent itself.
        pass


def _fallback_to_rules(
    source_text: str,
    overlay: dict,
    invocation: AgentInvocation,
    audit_path: Path,
    reason: str,
) -> AgentResult:
    """When the LLM tier is unreachable, fall back to deterministic
    parse_trigger. Per #29 fault-tolerant cascade — never crash because
    the LLM is unavailable. The operator gets a less-precise plan but
    something useful.

    The rules-tier output is wrapped in an AgentResult so callers
    don't need to special-case it. The proposed_plan will be None
    (the rules tier doesn't stage JSON), and operator_message
    explains the fallback so the operator knows what happened.
    """
    req = parse_trigger.parse(source_text, overlay)
    plan_steps = parse_trigger.derive_plan(req)
    invocation.final_text = (
        f"[rules-tier fallback] {reason}\n"
        f"Best-effort parse: customer={req.customer_hint!r}, "
        f"date_range={req.explicit_date_range}, "
        f"month_year={req.month_year}, currency={req.currency}"
    )
    _write_audit(audit_path, invocation)
    if not plan_steps:
        msg = (
            f"⚠️ Couldn't reach the LLM agent ({reason}). Tried a deterministic "
            f"parse but couldn't extract a customer + date window from the "
            f"trigger. Please rephrase with explicit dates and customer name, "
            f"e.g. 'file my INSEAD May 2026 receipts'."
        )
    else:
        msg = (
            f"⚠️ LLM agent unreachable ({reason}). Falling back to "
            f"deterministic parse:\n\n"
            f"• Customer: {req.customer_hint or 'unknown'}\n"
            f"• Date window: {req.explicit_date_range or req.month_year}\n"
            f"• Currency: {req.currency}\n\n"
            f"Plan length: {len(plan_steps)} steps. Operator approval still "
            f"required before any QB write."
        )
    return AgentResult(
        operator_message=msg,
        staged_plan_path=None,
        proposed_plan=None,
        invocation=invocation,
    )


def _generic_fallback_message() -> str:
    return (
        "I'm not sure what to do with that. Try a trigger like "
        "`file my INSEAD May 2026 receipts in EUR`, or include explicit "
        "dates: `find receipts for my Cornell trip 2026-04-08 to "
        "2026-04-12`."
    )
