"""general_assistant — Claude over email for arbitrary operator questions.

When the dispatcher classifies an incoming email as GENERAL_QUERY or
WORKFLOW_SUGGESTION, this module invokes Claude (Haiku by default,
Sonnet on escalation) and returns a polished reply text.

Two modes:

  respond_to_query(text, overlay) → str
      The operator asked a question. Claude answers it, possibly
      using `web_search` to look things up. Used for: research,
      jokes, opinions, factual questions, code help.

  propose_workflow(text, overlay) → str
      The operator described a workflow they wish existed. Claude
      writes a structured "here's what it could look like" proposal
      — but NEVER creates anything. Per SAI #9 the email channel is
      not a registered edit channel; only Co-Work / Claude Code can
      ship a new workflow.

Both modes:
  * Honor the daily LLM cap (lib.llm_costs.enforce_daily_cap)
  * Resolve the Anthropic key from macOS Keychain (no `op` prompts)
  * Log every call to ~/Library/Logs/SAI/llm_costs.jsonl
  * Audit every invocation to ~/Library/Logs/SAI/general_assistant.jsonl

Cap on tool-use iterations: 6 (matches cost_compiler_agent).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


MAX_ITERATIONS: int = 6
DEFAULT_MODEL: str = "claude-haiku-4-5"
AUDIT_PATH: Path = Path.home() / "Library" / "Logs" / "SAI" / "general_assistant.jsonl"

HAIKU_INPUT_PRICE_PER_MTOK: float = 1.00
HAIKU_OUTPUT_PRICE_PER_MTOK: float = 5.00


# ─── system prompts (kept inline; small enough that the hash-lock
#     migration in PRINCIPLES.md #24c can wait for a Phase E session) ──

_QUERY_SYSTEM_PROMPT = """\
You are a personal assistant the operator (Lutz) reaches via email at
sai@example.com. Today's email asked you a general question — not
a structured workflow request like cost-compilation.

This is CASE (a) of the three-case dispatcher: a known workflow ("answer
Lutz's question with Claude"). You acknowledge implicitly by answering;
the answer IS the status.

Your job:
  * Answer the question well.
  * Use the `web_search` tool when the answer needs current
    information you don't already know (news, prices, schedules,
    release notes, anything time-sensitive).
  * For jokes, opinions, brainstorms, code questions — answer
    directly without searching.

Output format — STRICT:
  * The reply is sent as a PLAIN-TEXT email. Lutz's clients (Gmail
    web, Superhuman) render plaintext bodies literally. So:
      - NO `**bold**`, `*italic*`, `__bold__`, `_italic_`.
      - NO `# Headers`, `## Headers`, `### Headers`.
      - NO `---` horizontal rules.
      - NO `[text](url)` link syntax — write the URL inline like
        `see https://example.com` instead.
      - NO ``` fenced code blocks or `inline code` backticks.
      - Plain dashes `- ` for bullets are fine. So is an UPPERCASE
        WORD now and then for emphasis.
  * Keep paragraphs short. The whole email should be at most
    ~100 words unless the operator's question genuinely needs more.
  * NEVER pretend you have done something you haven't. If you can't
    answer, say so.
  * NEVER reveal API keys, file paths, or other operational details
    of the SAI system. The operator knows where things live; the
    email is for the ANSWER.
  * Sign off naturally — no formal "Best regards" stuff. Be warm
    and direct.
  * Be specific. Vague answers waste the operator's time.
"""

_WORKFLOW_SUGGESTION_SYSTEM_PROMPT = """\
You are a personal assistant who has just received an email from the
operator (Lutz) describing a workflow they wish existed AND that SAI
cannot accomplish today with its existing tools. (If SAI could do it
with current tools, you would not be invoked — the AD_HOC handler
would propose concrete STEPS instead.)

This is CASE (b) of the three-case dispatcher: "no approved workflow,
no existing tools." Your reply MUST follow this exact three-section
template — same skeleton every time, only the content changes:

  1. One short line (UNDER 170 CHARACTERS) that opens with the
     literal phrase: "I don't have an approved workflow." Then a
     terse summary of what you COULD do for them if it existed,
     e.g. "I don't have an approved workflow. I could scan your
     calendar each Monday and propose 3 LinkedIn topics."

  2. Exactly one blank line, then a paragraph of AT MOST 100 WORDS
     describing what the workflow would look like — trigger, what
     it reads, what it produces, where approvals fit. Plain prose,
     no bullets unless they really help.

  3. Exactly one blank line, then the literal heading
     "CLAUDE CODE PROMPT:" on its own line, then a copy-pasteable
     prompt the operator can paste into Claude Code (or Co-Work)
     to actually build the workflow. The prompt should:
        - Name the workflow concretely.
        - Reference the relevant SAI directories
          (`SAI/workflows/`, `SAI/app/workers/`, etc.) when known.
        - List the key inputs / outputs / approval gate.
        - Be self-contained — the operator should be able to paste
          it without further context.

Then ONE final blank line and the literal closing line:
  "This is a proposal only — per SAI principle #9, new workflows
  ship through Co-Work or Claude Code, never through email."

Output format — STRICT:
  * The reply is sent as a PLAIN-TEXT email. NO `**bold**`,
    `## headers`, `---` rules, `[text](url)` link syntax,
    ``` fenced blocks, or `inline code`. Plain dashes for bullets
    are fine; an UPPERCASE LABEL like "CLAUDE CODE PROMPT:" is fine.
  * Keep the whole email tight — the 100-word cap on section 2 is
    a real cap, not a guideline.
  * Do not invent SAI features that don't exist. If you're unsure
    whether a primitive exists, say so in section 2.

Tone: warm, concrete, no buzzwords. Imagine writing to a smart
colleague who'll either run the Claude Code prompt or push back on
the design.
"""

_AD_HOC_PROPOSAL_SYSTEM_PROMPT = """\
You are a personal assistant who has just received an email from the
operator (Lutz) describing a task that does NOT match a registered SAI
workflow, BUT which SAI could accomplish today with its existing tools
(read-only Gmail search, read-only Drive search if connected,
read-only Granola search if connected, web search, plain reasoning).

This is CASE (c) of the three-case dispatcher: "no approved workflow,
but SAI has the tools." Your reply MUST follow this exact template —
same skeleton every time, only the content changes:

  1. The literal opening line:
       "TLDR: I don't have this as an approved workflow, but here's
       what I would do if you approve."

  2. One blank line, then the literal heading "STEPS:" on its own
     line, followed by a numbered list of concrete, read-only steps
     SAI would actually execute. Each step:
        - Names a specific tool surface (Gmail search, Drive search,
          Granola search, web search) — NOT a hypothetical capability.
        - Says what it will look for, in plain words.
        - Is read-only. NEVER propose a step that mutates Gmail
          labels, sends an email, edits a calendar event, modifies
          code/prompts/policies, or any other side effect.

  3. One blank line, then the literal closing line:
       "Approve y/n — reply with a single 'y' to run these steps,
       'n' to drop."

If the operator's request would genuinely need a write action
(sending email, booking a meeting, posting somewhere) to be
useful, DO NOT propose this template. Instead, in that single case,
respond with one line acknowledging the request and saying the
side-effect path needs the WORKFLOW_SUGGESTION (case b) route —
the dispatcher will re-route the next turn.

Output format — STRICT:
  * Plain text only. NO `**bold**`, `## headers`, `---` rules,
    `[text](url)` links, ``` fenced blocks, or `inline code`.
    Numbered list ("1. ...") is fine; plain dashes ("- ...") are fine.
  * Total length ≤ 200 words. The operator should be able to read
    it on a phone in under 30 seconds.
  * Never invent steps SAI cannot run. If you're not sure a
    connector exists (e.g. Granola), say so in the step
    ("if Granola search is connected — currently not configured").

Tone: concrete, accountable, short.
"""

_AD_HOC_EXECUTION_SYSTEM_PROMPT = """\
You are executing a previously-approved ad-hoc task on behalf of the
operator (Lutz). The previous email turn proposed STEPS and the
operator replied "y" (approve).

Your job:
  * Carry out the steps using the read-only tools you actually have
    available this turn (currently: web_search). If a step in the
    proposal referenced a tool that is not actually plugged in
    (Gmail search, Drive search, Granola search), report that
    honestly in the result rather than fabricating findings.
  * Produce a single status reply that mirrors CASE (a):
      - Open with the literal phrase: "Done." or
        "Partial — see below." (whichever is honest).
      - One blank line, then a paragraph of AT MOST 100 WORDS
        summarizing what you found and what's still missing.
  * NEVER take a write action (no email send, no calendar write,
    no label change, no file mutation).

Output format — STRICT:
  * Plain text. NO `**bold**`, `## headers`, `---` rules,
    `[text](url)` links, ``` fenced blocks, or `inline code`.
  * If a tool isn't available yet, say "TOOL_NOT_CONNECTED: <name>"
    in the body so the operator can see exactly where the gap is.
"""


@dataclass
class AssistantInvocation:
    invocation_id: str
    started_at: str
    mode: str                       # "query" | "workflow_suggestion"
    source_text: str
    model_used: str
    iterations: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    final_text: str = ""
    cost_usd: float = 0.0
    terminated_reason: str = "end_turn"
    error: Optional[str] = None


def _invocation_id() -> str:
    return f"ga_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


def _write_audit(invocation: AssistantInvocation) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "invocation_id": invocation.invocation_id,
            "started_at": invocation.started_at,
            "mode": invocation.mode,
            "source_text": invocation.source_text[:500],
            "model_used": invocation.model_used,
            "iterations": invocation.iterations,
            "tool_calls": invocation.tool_calls,
            "final_text": invocation.final_text[:1500],
            "cost_usd": round(invocation.cost_usd, 6),
            "terminated_reason": invocation.terminated_reason,
            "error": invocation.error,
        }
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _run_claude_loop(
    *,
    system_prompt: str,
    user_text: str,
    overlay: dict,
    mode: str,
    use_web_search: bool,
    model: Optional[str] = None,
) -> AssistantInvocation:
    """Generic Claude tool-use loop. Used by both modes."""
    invocation = AssistantInvocation(
        invocation_id=_invocation_id(),
        started_at=datetime.now(timezone.utc).isoformat(),
        mode=mode,
        source_text=user_text[:500],
        model_used=model or DEFAULT_MODEL,
    )

    # Lazy imports
    try:
        import anthropic
    except ImportError as e:
        invocation.terminated_reason = "error"
        invocation.error = f"anthropic SDK missing: {e}"
        _write_audit(invocation)
        return invocation

    from lib import op_env, llm_costs

    # Daily cap check (estimate $0.05 worst-case)
    try:
        llm_costs.enforce_daily_cap(
            skill="receipt-collector",
            step=f"general_assistant.{mode}",
            upcoming_usd_cost=0.05,
            overlay=overlay,
        )
    except llm_costs.BudgetExceeded as bx:
        invocation.terminated_reason = "budget_exceeded"
        invocation.error = str(bx)
        _write_audit(invocation)
        return invocation

    api_key = op_env.get_cached_secret("anthropic")
    if not api_key:
        invocation.terminated_reason = "error"
        invocation.error = "no Anthropic key cached (run `runner cache-secrets`)"
        _write_audit(invocation)
        return invocation

    chosen_model = (model
                    or os.environ.get("SAI_GENERAL_ASSISTANT_MODEL")
                    or DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=api_key)

    tools_arg: list = []
    if use_web_search:
        # Anthropic's server-side web_search tool. Each search is a
        # separate billable usage event tracked under the model call.
        tools_arg.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        })

    messages: list[dict] = [{"role": "user", "content": user_text}]
    iteration = 0
    final_text = ""

    while iteration < MAX_ITERATIONS:
        iteration += 1
        try:
            kwargs = dict(
                model=chosen_model,
                max_tokens=2048,
                system=system_prompt,
                messages=messages,
            )
            if tools_arg:
                kwargs["tools"] = tools_arg
            resp = client.messages.create(**kwargs)
        except Exception as e:
            invocation.terminated_reason = "error"
            invocation.error = f"anthropic call failed: {e}"
            break

        # Cost accounting
        in_tok = getattr(resp.usage, "input_tokens", 0) or 0
        out_tok = getattr(resp.usage, "output_tokens", 0) or 0
        call_cost = (in_tok * HAIKU_INPUT_PRICE_PER_MTOK
                     + out_tok * HAIKU_OUTPUT_PRICE_PER_MTOK) / 1_000_000
        invocation.cost_usd += call_cost
        llm_costs.log_call(
            skill="receipt-collector",
            step=f"general_assistant.{mode}",
            model=chosen_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            usd_cost=call_cost,
            note=f"iter={iteration} stop_reason={resp.stop_reason}",
        )

        # Capture any text output
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                final_text = block.text

        # Handle tool_use (only web_search in this loop)
        tool_uses = [b for b in (resp.content or [])
                     if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            invocation.terminated_reason = "end_turn"
            break

        # Append assistant turn + synthesize tool results for the
        # web_search server tool. Per Anthropic docs the server tool
        # auto-completes; we just need to append the model's content
        # and the next user turn is the tool_result it generated.
        # For server tools, the API returns the search result inline
        # in subsequent model content, so we essentially loop until
        # we get a non-tool_use stop_reason.
        messages.append({"role": "assistant", "content": resp.content})
        # For server tools (like web_search_20250305), Anthropic
        # handles the round-trip internally; we just need to continue
        # the conversation by sending an empty user message ack so the
        # model proceeds. In practice, the model's NEXT response
        # carries the search result + the final answer in one turn.
        # If the stop_reason was "tool_use", the API expects us to
        # signal we're ready for the model's continuation. For server
        # tools, we just re-call messages.create with the assistant
        # response in history and no new user content needed.
        # Track the tool use for audit.
        for tu in tool_uses:
            invocation.tool_calls.append({
                "iter": iteration,
                "tool": getattr(tu, "name", "?"),
                "id": getattr(tu, "id", ""),
            })

    else:
        invocation.terminated_reason = "iteration_cap"

    invocation.iterations = iteration
    invocation.final_text = final_text or "(no response — try rephrasing your question)"
    _write_audit(invocation)
    return invocation


def respond_to_query(text: str, overlay: dict, *,
                     model: Optional[str] = None) -> str:
    """Reply to a general question. Returns the email body text."""
    inv = _run_claude_loop(
        system_prompt=_QUERY_SYSTEM_PROMPT,
        user_text=text,
        overlay=overlay,
        mode="query",
        use_web_search=True,
        model=model,
    )
    return inv.final_text


def propose_workflow(text: str, overlay: dict, *,
                     model: Optional[str] = None) -> str:
    """CASE (b) — articulate what a new workflow could look like,
    using the three-section template (headline / 100-word /
    CLAUDE CODE PROMPT). NEVER creates anything (per SAI #9).
    Returns the email body text.

    The prompt itself enforces the closing principle-#9 line, so we
    no longer append it here (avoids doubling when the model already
    obeyed)."""
    inv = _run_claude_loop(
        system_prompt=_WORKFLOW_SUGGESTION_SYSTEM_PROMPT,
        user_text=text,
        overlay=overlay,
        mode="workflow_suggestion",
        use_web_search=False,
        model=model,
    )
    body = inv.final_text or ""
    if "principle #9" not in body.lower():
        body = body.rstrip() + (
            "\n\nThis is a proposal only — per SAI principle #9, new "
            "workflows ship through Co-Work or Claude Code, never through "
            "email."
        )
    return body


def propose_ad_hoc_steps(text: str, overlay: dict, *,
                         model: Optional[str] = None) -> str:
    """CASE (c) — SAI has the tools to handle this request, but it's
    not a registered workflow.

    2026-05-28: AUTO-EXECUTE the read-only context-gathering NOW (one
    turn), then propose only the write step for approval — instead of
    the old propose-everything-then-execute-on-y two-turn flow. The
    decomposed path runs gmail_search / forbes_latest immediately
    (free, no side effect) and substitutes the findings into the
    reply, so the operator sees evidence + a single propose-for-
    approval draft step. Per PRINCIPLES.md #20 the write step is
    proposed, never auto-run.

    Falls back to the old propose-only template when the task does
    NOT decompose (decomposed path returns None)."""
    try:
        from lib import ad_hoc_decomposed
        decomposed = ad_hoc_decomposed.propose_decomposed(
            text=text,
            overlay=overlay,
            claude_loop_fn=_run_claude_loop,
            model=model,
        )
        if decomposed:
            return decomposed
    except Exception:
        # Any failure in the decomposed path → fall back to the old
        # propose-only flow (fail-closed to the safe, known behavior).
        pass

    inv = _run_claude_loop(
        system_prompt=_AD_HOC_PROPOSAL_SYSTEM_PROMPT,
        user_text=text,
        overlay=overlay,
        mode="ad_hoc_proposal",
        use_web_search=False,
        model=model,
    )
    return inv.final_text or ""


def execute_ad_hoc_steps(approved_proposal: str, original_request: str,
                         overlay: dict, *,
                         model: Optional[str] = None) -> str:
    """CASE (c) — run the previously-approved STEPS using whatever
    read-only tools are actually wired up this turn. Returns the
    status reply (mirrors CASE (a) shape: short headline + ≤100-word
    explanation)."""
    user_text = (
        "Original operator request:\n"
        f"{original_request.strip()}\n\n"
        "Approved proposal (the operator replied 'y'):\n"
        f"{approved_proposal.strip()}\n\n"
        "Execute the read-only STEPS now and produce the case-(a) "
        "status reply."
    )
    inv = _run_claude_loop(
        system_prompt=_AD_HOC_EXECUTION_SYSTEM_PROMPT,
        user_text=user_text,
        overlay=overlay,
        mode="ad_hoc_execution",
        use_web_search=True,
        model=model,
    )
    return inv.final_text or "Partial — execution returned no text."
