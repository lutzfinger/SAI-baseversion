"""sai-eval agent runner — LangChain edition.

Architecture (PRINCIPLES.md §12 cascade applied to operator input):

  operator message
    │
    ├─[regex parsers]──MATCH───────→ stage proposal → ✅/❌
    │                  (instant, no LLM cost)
    │
    └─[this agent]──── NO MATCH ──→ LangChain agent with the tool surface
                                    declared in tools.py + Anthropic Claude.
                                    Read-only tools + propose-only tools.
                                    Same two-phase commit at the end.

LangChain choice: SAI was originally built on LangChain; per principle
#24a (Open framework, single API surface) we use it across the
codebase rather than vendor-specific SDKs. The agent loop, tool
registration, retry, schema generation, and provider abstraction all
come from LangChain — we ship the prompt + tools + supervisory caps.

Anthropic Claude choice: per #24a "one API key for the operator if at
all possible" — Co-Work uses Claude, so the operator already has an
ANTHROPIC_API_KEY. The framework defaults to that same provider for
its internal agents.

Supervisory layer:

  * Iteration cap (`MAX_ITERATIONS`) via LangChain's recursion_limit.
  * Audit log row per invocation captured via a LangChain callback.
  * Two-phase commit gate is the operator ✅; nothing here writes to
    rules / canaries / edge_cases — only `propose_*` tools that stage
    YAML proposals.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from app.agents.tools import REGISTERED_TOOL_SPECS, ToolContext, build_tools
from app.llm.registry import UnknownLLMRole, get_model_for_role
from app.shared.prompt_loader import PromptHashMismatch, load_hashed_prompt

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT_RELPATH: str = "agents/sai_eval_agent.md"
"""Hash-locked path inside ``prompts/`` (see prompts/prompt-locks.yaml).
Per PRINCIPLES.md §24c the system prompt MUST live in a file and be
loaded through the hash-verifying loader; inline strings are a
violation."""

LLM_ROLE: str = "agent_default"
"""Logical role looked up in ``config/llm_registry.yaml``. Per
PRINCIPLES.md §24b code references LLMs by role, never by literal
model id. To swap models, edit the registry — no code change."""

# ─── supervisory limits ───────────────────────────────────────────────

MAX_ITERATIONS: int = 8
"""Hard cap on agent steps per invocation. LangChain's recursion_limit
is set to MAX_ITERATIONS * 2 (each step = LLM call + optional tool
call, so 8 logical steps ≈ 16 graph nodes)."""

DEFAULT_AUDIT_PATH: Path = (
    Path.home() / "Library" / "Logs" / "SAI" / "sai_eval_agent.jsonl"
)


# ─── system prompt ────────────────────────────────────────────────────
#
# Per PRINCIPLES.md §24c, the system prompt lives in a hash-locked
# file (``prompts/agents/sai_eval_agent.md``) and is loaded through the
# verifying loader. Inline strings here would be a violation. The
# loader fails closed on hash mismatch — there is no fallback to a
# stale inline copy.


# ─── audit row + result types ─────────────────────────────────────────


@dataclass
class AgentInvocation:
    invocation_id: str
    started_at: str
    operator_user_id: str
    source_text: str
    model_used: str
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    full_tool_calls: list[dict[str, Any]] = field(default_factory=list)  # untruncated, for RL
    final_text: str = ""
    final_proposal_id: Optional[str] = None
    cost_usd: float = 0.0
    terminated_reason: str = "end_turn"  # end_turn | proposed | iteration_cap | error
    error: Optional[str] = None


@dataclass
class AgentResult:
    operator_message: str
    staged_proposal_path: Optional[str] = None
    invocation: Optional[AgentInvocation] = None


# ─── runner ───────────────────────────────────────────────────────────


def run_agent(
    *,
    operator_user_id: str,
    source_text: str,
    proposed_dir: Path,
    gmail_authenticator: Any,
    llm: Any = None,
    model: Optional[str] = None,
    audit_path: Optional[Path] = None,
    intent_context: Optional[str] = None,
    progress_poster: Optional[Any] = None,
) -> AgentResult:
    """Run one agent turn over `source_text`. Returns AgentResult.

    `llm` is injected for tests. Production callers pass None and we
    build `ChatAnthropic` from env (`ANTHROPIC_API_KEY`).

    `intent_context` (optional) — when re-invoking under an open
    pending intent (PRINCIPLES.md §16g), pass the prior-attempts
    summary so the agent doesn't re-propose a rejected shape.

    `progress_poster` (optional) — Callable[[str], None]. Called on
    every tool start with a short human-readable progress line
    (e.g. "🔧 calling `search_gmail`…"). Slack handler passes a
    closure that posts to the active thread; tests pass None.
    Closes MVP-GAPS Gap 14 (async UX for the agent's tool calls).
    The callable MUST be non-blocking and exception-safe — handler
    swallows any error so a Slack outage doesn't break the agent.
    """

    # Per #24b: model id comes from the registry by role. SAI_AGENT_MODEL
    # is a one-off env override (mostly tests + ad-hoc operator
    # experiments); production reads config/llm_registry.yaml.
    if model is not None:
        chosen_model = model
    else:
        try:
            chosen_model = get_model_for_role(
                LLM_ROLE,
                env_override=os.environ.get("SAI_AGENT_MODEL"),
            )
        except UnknownLLMRole:
            # Fail closed per #6: if the registry is missing the role,
            # the agent refuses rather than silently falling back to a
            # hardcoded default. The boundary linter would catch any
            # such fallback as a #24b violation.
            raise

    invocation = AgentInvocation(
        invocation_id=_invocation_id(),
        started_at=datetime.now(UTC).isoformat(),
        operator_user_id=operator_user_id,
        source_text=source_text,
        model_used=chosen_model,
    )
    audit_path = audit_path or DEFAULT_AUDIT_PATH

    try:
        system_prompt = load_hashed_prompt(SYSTEM_PROMPT_RELPATH)
    except PromptHashMismatch as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"Prompt hash mismatch: {exc}"
        _write_audit(audit_path, invocation)
        return AgentResult(
            operator_message=(
                "My system prompt failed hash verification — refusing to "
                "run rather than use untrusted instructions. Re-merge the "
                "runtime (`sai-overlay merge`) or refresh "
                "`prompts/prompt-locks.yaml` after reviewing the change."
            ),
            invocation=invocation,
        )

    try:
        llm = llm or _build_llm(chosen_model)
    except Exception as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"LLM unavailable: {exc}"
        _write_audit(audit_path, invocation)
        return AgentResult(
            operator_message=(
                "I can't reach my reasoning model right now "
                "(ANTHROPIC_API_KEY may not be set). Try the canonical "
                "`add rule: …` / `… should be …` formats which don't "
                "need it, or set the key in `~/.config/sai/runtime.env`."
            ),
            invocation=invocation,
        )

    ctx = ToolContext(
        proposed_by=operator_user_id,
        source_text=source_text,
        proposed_dir=proposed_dir,
        gmail_authenticator=gmail_authenticator,
        cache={},
    )
    tools = build_tools(ctx)

    # Build the agent. Per LangChain 1.x, `create_agent` returns a
    # graph that we invoke with a messages list.
    from langchain.agents import create_agent

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    # Track tool calls + costs via callback. LangChain emits
    # on_tool_start / on_tool_end / on_llm_end events. The handler is
    # a BaseCallbackHandler subclass (built lazily so we don't import
    # LangChain at module-load time).
    handler = _audit_handler_class()(invocation, progress_poster=progress_poster)

    # When re-invoking under an open pending intent, prepend the
    # prior-attempts context so the agent can adjust shape rather
    # than repeat the same rejected proposal (#16g).
    user_content = source_text
    if intent_context:
        user_content = f"{intent_context}\n\n── Operator's latest message ──\n{source_text}"

    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": user_content}]},
            config={
                "recursion_limit": MAX_ITERATIONS * 2,
                "callbacks": [handler],
            },
        )
    except Exception as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"agent.invoke failed: {exc}"
        _write_audit(audit_path, invocation)
        return AgentResult(
            operator_message=(
                "Something went wrong reaching my model. Try the "
                "canonical formats and I'll handle the rest."
            ),
            invocation=invocation,
        )

    invocation.iterations = handler.iterations or 1

    # Walk the message list to extract: final text + last propose_*
    # tool result (if any).
    messages = result.get("messages", [])
    final_text = ""
    staged_path: Optional[str] = None
    operator_message_override: Optional[str] = None
    final_proposal_id: Optional[str] = None

    for msg in messages:
        # The terminal AIMessage has the agent's final text.
        if _is_ai_message(msg) and _msg_content(msg):
            final_text = _msg_content(msg)
        # Look at tool results for any propose_* output.
        if _is_tool_message(msg):
            tool_name = _msg_tool_name(msg)
            if tool_name in ("propose_classifier_rule", "propose_llm_example"):
                payload = _parse_tool_output(_msg_content(msg))
                if isinstance(payload, dict) and payload.get("staged_path"):
                    staged_path = payload["staged_path"]
                    operator_message_override = payload.get("operator_message", "")
                    final_proposal_id = payload.get("proposal_id")

    # Decision: if a propose tool succeeded, the tool's operator_message
    # is the canonical user-facing text (not the LLM's elaboration).
    if operator_message_override:
        final_text = operator_message_override
        invocation.terminated_reason = "proposed"

    invocation.final_text = final_text or _fallback()
    invocation.final_proposal_id = final_proposal_id
    _write_audit(audit_path, invocation)
    _try_capture_trajectory(invocation, system_prompt, audit_path)

    return AgentResult(
        operator_message=final_text or _fallback(),
        staged_proposal_path=staged_path,
        invocation=invocation,
    )


# ─── helpers ──────────────────────────────────────────────────────────


def _build_llm(model: str) -> Any:
    """Build the Anthropic Claude LLM."""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=model, api_key=api_key, temperature=0.0, max_tokens=2048,
    )


def _invocation_id() -> str:
    return f"agent_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


def _is_ai_message(msg: Any) -> bool:
    cls = msg.__class__.__name__
    return cls in ("AIMessage", "AIMessageChunk")


def _is_tool_message(msg: Any) -> bool:
    return msg.__class__.__name__ == "ToolMessage"


def _msg_content(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        # Anthropic content blocks — concatenate text blocks.
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _msg_tool_name(msg: Any) -> str:
    return str(getattr(msg, "name", "") or "")


def _parse_tool_output(content: str) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _fallback() -> str:
    return (
        "I'm not sure what to do with that. This channel is for "
        "classification feedback — try `add rule: <sender> → <label>` "
        "or `<sender> should be <label>`."
    )


# ─── LangChain callback for audit + cost tracking ─────────────────────


def _audit_handler_class():
    """Build the audit handler subclass at runtime so we get a clean
    subclass of LangChain's BaseCallbackHandler (with all the required
    `ignore_*` / `raise_error` attributes)."""

    from langchain_core.callbacks.base import BaseCallbackHandler

    class _AuditHandler(BaseCallbackHandler):
        def __init__(
            self,
            invocation: AgentInvocation,
            *,
            progress_poster: Any = None,
        ):
            super().__init__()
            self.invocation = invocation
            self.iterations = 0
            self.progress_poster = progress_poster

        def on_llm_end(self, response: Any, *args: Any, **kwargs: Any) -> None:
            return _audit_on_llm_end(self, response)

        def on_tool_start(
            self, serialized: dict, input_str: str, *args: Any, **kwargs: Any,
        ) -> None:
            _audit_on_tool_start(self, serialized, input_str)
            # Async UX (Gap 14): notify the operator that a tool is
            # firing. Errors are swallowed — the agent must keep
            # running even if Slack is down.
            if self.progress_poster is not None:
                tool_name = (serialized or {}).get("name", "?")
                args_short = (str(input_str) or "")[:120]
                line = f":hourglass_flowing_sand: calling `{tool_name}` …"
                if args_short and args_short not in ("{}", "None"):
                    line = f"{line} (args: `{args_short}`)"
                try:
                    self.progress_poster(line)
                except Exception:
                    pass

        def on_tool_end(self, output: Any, *args: Any, **kwargs: Any) -> None:
            _audit_on_tool_end(self, output)
            if self.progress_poster is not None:
                # Surface the tool's return at a glance: dict → key
                # summary; string → first line; otherwise the type.
                summary = _summarize_tool_output(output)
                try:
                    self.progress_poster(f":white_check_mark: tool returned: {summary}")
                except Exception:
                    pass

        def on_tool_error(
            self, error: Exception, *args: Any, **kwargs: Any,
        ) -> None:
            _audit_on_tool_error(self, error)
            if self.progress_poster is not None:
                try:
                    self.progress_poster(
                        f":x: tool error: `{type(error).__name__}: {error}`"
                    )
                except Exception:
                    pass

    return _AuditHandler


def _summarize_tool_output(output: Any) -> str:
    """One-line glance at a tool's return value for Slack progress posts."""
    if output is None:
        return "(no result)"
    if isinstance(output, dict):
        # Highlight common useful keys; fall back to keys list.
        for key in ("staged_path", "n_results", "operator_message", "candidates"):
            if key in output:
                val = output[key]
                if isinstance(val, list):
                    return f"{key}=[{len(val)} items]"
                return f"{key}={str(val)[:80]}"
        return "{" + ", ".join(sorted(output.keys())[:4]) + "}"
    s = str(output)
    first_line = s.split("\n", 1)[0][:140]
    return first_line or f"({type(output).__name__})"


def _audit_on_llm_end(handler: Any, response: Any) -> None:
    handler.iterations += 1
    try:
        generations = getattr(response, "generations", None) or []
        for gen_list in generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                usage = getattr(msg, "usage_metadata", {}) or {}
                in_t = int(usage.get("input_tokens", 0) or 0)
                out_t = int(usage.get("output_tokens", 0) or 0)
                # claude-haiku-4-5 pricing: ~$1/$5 per 1M tokens
                handler.invocation.cost_usd += (
                    in_t * 1.0 + out_t * 5.0
                ) / 1_000_000
    except Exception:
        pass


def _audit_on_tool_start(handler: Any, serialized: dict, input_str: str) -> None:
    name = (serialized or {}).get("name", "?")
    now = datetime.now(UTC).isoformat()
    handler.invocation.tool_calls.append({
        "tool": name,
        "args_truncated": str(input_str)[:600],
        "at": now,
        "result_truncated": "(pending)",
    })
    handler.invocation.full_tool_calls.append({
        "tool": name,
        "args": str(input_str),
        "at": now,
        "result": "(pending)",
        "error": None,
    })


def _audit_on_tool_end(handler: Any, output: Any) -> None:
    if handler.invocation.tool_calls:
        handler.invocation.tool_calls[-1]["result_truncated"] = str(output)[:1500]
    if handler.invocation.full_tool_calls:
        handler.invocation.full_tool_calls[-1]["result"] = str(output)


def _audit_on_tool_error(handler: Any, error: Exception) -> None:
    msg = f"ERROR: {type(error).__name__}: {error}"
    if handler.invocation.tool_calls:
        handler.invocation.tool_calls[-1]["result_truncated"] = msg
    if handler.invocation.full_tool_calls:
        handler.invocation.full_tool_calls[-1]["result"] = msg
        handler.invocation.full_tool_calls[-1]["error"] = msg


def _try_capture_trajectory(
    invocation: AgentInvocation,
    system_prompt: str,
    audit_path: Path,
) -> None:
    """Write a RawTrajectory alongside the audit row. Non-fatal — swallows all errors."""
    try:
        from datetime import UTC, datetime as _dt

        from app.rl.models import TrajectoryStep
        from app.rl.trajectory import RawTrajectory, TrajectoryStore

        steps = [
            TrajectoryStep(
                tool_name=tc.get("tool", "?"),
                args=tc.get("args", ""),
                result=tc.get("result", ""),
                at=_dt.fromisoformat(tc.get("at", _dt.now(UTC).isoformat())),
                error=tc.get("error"),
            )
            for tc in invocation.full_tool_calls
        ]

        trajectory = RawTrajectory(
            invocation_id=invocation.invocation_id,
            workflow_id="sai-eval",
            system_prompt=system_prompt,
            user_message=invocation.source_text,
            steps=steps,
            final_response=invocation.final_text,
            model_used=invocation.model_used,
            cost_usd=invocation.cost_usd,
            started_at=_dt.fromisoformat(invocation.started_at),
            completed_at=_dt.now(UTC),
            terminated_reason=invocation.terminated_reason,
        )

        store = TrajectoryStore(root=audit_path.parent / "trajectories")
        store.append(trajectory)
    except Exception as exc:
        LOGGER.debug("RL trajectory capture skipped: %s", exc)


def _write_audit(audit_path: Path, invocation: AgentInvocation) -> None:
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "invocation_id": invocation.invocation_id,
            "started_at": invocation.started_at,
            "operator_user_id": invocation.operator_user_id,
            "source_text": invocation.source_text[:500],
            "model_used": invocation.model_used,
            "iterations": invocation.iterations,
            "tool_calls": invocation.tool_calls,
            "tool_specs": [
                {"name": s.name, "rights": s.rights}
                for s in REGISTERED_TOOL_SPECS
            ],
            "final_text": invocation.final_text[:1000],
            "final_proposal_id": invocation.final_proposal_id,
            "cost_usd": round(invocation.cost_usd, 6),
            "terminated_reason": invocation.terminated_reason,
            "error": invocation.error,
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:
        LOGGER.warning("agent audit write failed: %s", exc)
