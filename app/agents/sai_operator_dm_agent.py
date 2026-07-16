"""sai-operator DM agent — conversational command surface.

Architecture (PRINCIPLES.md §16f + §12 cascade):

  operator DM message
    │
    └─[this agent]── LangChain agent w/ guarded tool surface ──
                     • list_available_skills (read-only)
                     • propose_skill_run     (propose-only — stages a
                                               YAML proposal for ✅)
                     • clarify_intent        (asks for missing info)

Unlike `sai_eval_agent` (which has a regex Tier-0 first), DM messages
are conversational — they go STRAIGHT to the LLM. Per PRINCIPLES.md
§16f the agent's tool surface IS the guardrail; the agent can read
operator intent, ask clarifying questions, and stage proposals only.
Execution requires operator ✅ via the existing two-phase commit.

LLM choice: per #24b the default for human-facing interactive surfaces
is CLOUD MEDIUM (the `agent_default` role — resolved from the registry, not
named here). A future iteration will add a local Tier-1 (the `cascade_local`
role) with cloud as Tier-2 fallback per #12 cascade; v0.1 ships cloud-only.

Per #24c the system prompt is hash-locked at
`prompts/agents/sai_operator_dm_agent.md`.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.llm.registry import UnknownLLMRole, get_model_for_role

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT_RELPATH: str = "agents/sai_operator_dm_agent.md"
LLM_ROLE: str = "agent_default"  # claude-haiku-4-5 per #24b interactive default

MAX_ITERATIONS: int = 6   # Conversational agent — usually 1-2 turns suffice.

DEFAULT_AUDIT_PATH: Path = (
    Path.home() / "Library" / "Logs" / "SAI" / "sai_operator_dm_agent.jsonl"
)


# ─── result types ────────────────────────────────────────────────────

@dataclass
class DmAgentInvocation:
    invocation_id: str
    started_at: str
    operator_user_id: str
    source_text: str
    model_used: str
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    staged_proposal_path: Optional[str] = None
    cost_usd: float = 0.0
    terminated_reason: str = "end_turn"  # end_turn | proposed | iteration_cap | error
    error: Optional[str] = None


@dataclass
class DmAgentResult:
    operator_message: str
    staged_proposal_path: Optional[str] = None
    invocation: Optional[DmAgentInvocation] = None


# ─── tool implementations ───────────────────────────────────────────

def _list_available_skills() -> dict[str, Any]:
    """Return the catalog of skills the operator can invoke via DM."""
    try:
        from app.skills.skill_apply_registry import list_registered_workflows
        skills = list_registered_workflows()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "skills": []}
    return {
        "ok": True,
        "skills": [
            {
                "workflow_id": s,
                "trigger": f"Say something like 'do the {s.replace('-', ' ')}' "
                           f"with the params it needs.",
            }
            for s in skills
        ],
    }


def _invoke_skill_cascade(
    *,
    workflow_id: str,
    folder: Optional[str] = None,
    date_range: Optional[str] = None,
    sheet_url: Optional[str] = None,
    operator_user_id: str = "",
    extra_inputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Load the skill's runner.py and call its cascade with the parsed params.

    Returns {ok, workflow_id, final_verdict, proposal_path, audit_log, error?}.
    The cascade ends with a `human` tier that stages a YAML proposal at
    `~/.sai-runtime/eval/proposed/<workflow_id>/<thread_id>.yaml`.
    """
    try:
        from app.skills.skill_apply_registry import is_registered
    except Exception as exc:
        return {"ok": False, "error": f"registry import failed: {exc}"}
    if not is_registered(workflow_id):
        return {"ok": False, "error": f"workflow '{workflow_id}' not in registry"}

    import importlib.util as _ilu
    sai_public = Path(
        os.environ.get("SAI_PUBLIC_ROOT",
                       str(Path(__file__).resolve().parents[2]))
    )
    runner_path = sai_public / "skills" / workflow_id / "runner.py"
    if not runner_path.exists():
        # Try the runtime-merged location as fallback
        runner_path = (Path.home() / ".sai-runtime" / "skills" /
                       workflow_id / "runner.py")
    if not runner_path.exists():
        return {"ok": False, "error": f"runner not found at {runner_path}"}

    mod_name = f"_dm_runner_{workflow_id.replace('-', '_')}"
    import sys as _sys
    if mod_name in _sys.modules:
        runner_mod = _sys.modules[mod_name]
    else:
        spec = _ilu.spec_from_file_location(mod_name, runner_path)
        runner_mod = _ilu.module_from_spec(spec)
        _sys.modules[mod_name] = runner_mod
        spec.loader.exec_module(runner_mod)

    inputs = {
        "folder_name": folder,
        "folder": folder,            # back-compat alias for some validators
        "date_range": date_range or "all",
        "sheet_url": sheet_url,
        "thread_id": f"dm-{secrets.token_hex(6)}",
        **(extra_inputs or {}),
    }
    try:
        result = runner_mod.run(inputs)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "workflow_id": workflow_id}

    return {
        "ok": result.final_verdict == "ready_to_propose",
        "workflow_id": workflow_id,
        "final_verdict": result.final_verdict,
        "final_reason": result.final_reason,
        "proposal_path": result.proposal_path,
        "audit_log": result.audit_log,
        "summary": {
            "new_session_columns": result.accumulated.get("new_session_columns"),
            "transcripts_loaded": result.accumulated.get("transcripts_count"),
            "csv_path": result.accumulated.get("csv_path"),
        },
    }


def _build_dm_tools(operator_user_id: str) -> list[Any]:
    """Return the LangChain tools for the DM agent.

    Tool rights (per #16f):
      list_available_skills  — read_only
      propose_skill_run      — propose_only (stages a YAML proposal;
                                 actual side effects gated by ✅)
    """
    # Match the canonical SAI import path (app/agents/tools.py:38).
    # `langchain.tools.StructuredTool` was removed in newer LangChain;
    # `langchain_core.tools.StructuredTool` is the supported home.
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field as PField

    class ListSkillsInput(BaseModel):
        """No arguments."""
        pass

    def _list_skills_handler(**_kwargs: Any) -> str:
        result = _list_available_skills()
        return json.dumps(result)

    list_tool = StructuredTool.from_function(
        func=_list_skills_handler,
        name="list_available_skills",
        description=(
            "List the skills the operator can ask SAI to run. Returns a "
            "JSON dict with `skills: [{workflow_id, trigger}]`. Call this "
            "FIRST when you're unsure which skill matches the operator's "
            "intent. Read-only."
        ),
        args_schema=ListSkillsInput,
    )

    class ProposeSkillRunInput(BaseModel):
        workflow_id: str = PField(description=(
            "The skill to invoke (e.g. 'student-participation-check'). "
            "Must be in `list_available_skills` output."
        ))
        folder: str = PField(description=(
            "The Granola folder to pull recordings from "
            "(e.g. 'C-Suites May 2026 INSEAD'). Required for any skill that "
            "consumes Granola transcripts."
        ))
        date_range: str = PField(default="all", description=(
            "Date range as 'YYYY-MM-DD:YYYY-MM-DD' or 'all'. "
            "Default 'all' = every session in the folder."
        ))
        sheet_url: str = PField(description=(
            "Google Sheet URL where SAI should write the result. "
            "Operator must provide this — don't fabricate."
        ))

    def _propose_skill_run_handler(
        workflow_id: str, folder: str, sheet_url: str,
        date_range: str = "all",
    ) -> str:
        result = _invoke_skill_cascade(
            workflow_id=workflow_id, folder=folder,
            date_range=date_range, sheet_url=sheet_url,
            operator_user_id=operator_user_id,
        )
        return json.dumps(result)

    propose_tool = StructuredTool.from_function(
        func=_propose_skill_run_handler,
        name="propose_skill_run",
        description=(
            "Stage a proposal to run a SAI skill. Use this ONLY when you "
            "have all required parameters (workflow_id, folder, sheet_url) "
            "confirmed by the operator. The tool invokes the skill's "
            "cascade which stages a YAML proposal under "
            "`~/.sai-runtime/eval/proposed/<workflow_id>/<thread_id>.yaml`. "
            "The operator must then react ✅ on the slack message to "
            "actually fire the skill's side effects (e.g. writing to the "
            "Google Sheet). Returns `proposal_path` on success or an error."
        ),
        args_schema=ProposeSkillRunInput,
    )

    return [list_tool, propose_tool]


# ─── public entry point ─────────────────────────────────────────────

def run_dm_agent(
    *,
    operator_user_id: str,
    source_text: str,
    llm: Any = None,
    model: Optional[str] = None,
    audit_path: Optional[Path] = None,
    progress_poster: Optional[Callable[[str], None]] = None,
) -> DmAgentResult:
    """Run one DM-agent turn. Returns DmAgentResult."""
    # Resolve model (#24b)
    if model is not None:
        chosen_model = model
    else:
        try:
            chosen_model = get_model_for_role(
                LLM_ROLE,
                env_override=os.environ.get("SAI_DM_AGENT_MODEL"),
            )
        except UnknownLLMRole:
            raise

    invocation = DmAgentInvocation(
        invocation_id=secrets.token_hex(8),
        started_at=datetime.now(UTC).isoformat(),
        operator_user_id=operator_user_id,
        source_text=source_text,
        model_used=chosen_model,
    )
    audit_path = audit_path or DEFAULT_AUDIT_PATH

    # Load hash-locked system prompt (#24c)
    try:
        from app.shared.prompt_loader import (
            PromptHashMismatch, load_hashed_prompt,
        )
        system_prompt = load_hashed_prompt(SYSTEM_PROMPT_RELPATH)
    except PromptHashMismatch as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"Prompt hash mismatch: {exc}"
        _write_audit(audit_path, invocation)
        return DmAgentResult(
            operator_message=(
                "I'm paused — a safety check says my instructions don't "
                "match the version that was last reviewed. Try again after "
                "`sai-overlay merge` or refreshing `prompts/prompt-locks.yaml`."
            ),
            invocation=invocation,
        )
    except FileNotFoundError as exc:
        # During v0.1 the prompt may not be hash-locked yet — fall back
        # gracefully with a tracked warning, never silent.
        LOGGER.warning("prompt not hash-locked yet: %s — using direct read", exc)
        prompt_path = Path.home() / ".sai-runtime" / "prompts" / SYSTEM_PROMPT_RELPATH
        if not prompt_path.exists():
            prompt_path = Path(os.environ.get(
                "SAI_PUBLIC_ROOT",
                str(Path(__file__).resolve().parents[2]),
            )) / "prompts" / SYSTEM_PROMPT_RELPATH
        system_prompt = prompt_path.read_text()

    # Build LLM
    try:
        llm = llm or _build_llm(chosen_model)
    except Exception as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"LLM unavailable: {exc}"
        _write_audit(audit_path, invocation)
        return DmAgentResult(
            operator_message=(
                "I can't reach my reasoning model right now. "
                "Check that ANTHROPIC_API_KEY is set in "
                "`~/.config/sai/runtime.env`."
            ),
            invocation=invocation,
        )

    tools = _build_dm_tools(operator_user_id)

    from langchain.agents import create_agent
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    # NOTE: an earlier version wired `progress_poster` into a
    # LangChain BaseCallbackHandler so we could surface
    # "🔨 calling <tool>…" lines during agent work. That deadlocked the
    # slack_bolt worker thread (0% CPU on the main event loop, sample
    # showed acquire_timed on a Python Lock). Disabled until we move
    # the slack post off the LangChain callback thread (e.g. via a
    # background queue). The :eyes:/:✅: reactions in the slack handler
    # still provide the "we got it" signal; long tool calls just don't
    # surface intermediate progress for now.
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": source_text}]},
            config={"recursion_limit": MAX_ITERATIONS * 2},
        )
    except Exception as exc:
        invocation.terminated_reason = "error"
        invocation.error = f"agent.invoke failed: {exc}"
        _write_audit(audit_path, invocation)
        return DmAgentResult(
            operator_message=(
                "Something went wrong reaching my model. Try a more "
                "specific message like "
                "'run student participation check for <folder>, <dates>, <sheet>'."
            ),
            invocation=invocation,
        )

    # Walk messages to find final text + any propose_skill_run tool output
    messages = result.get("messages", [])
    final_text = ""
    staged_path: Optional[str] = None
    for msg in messages:
        if hasattr(msg, "content"):
            content = msg.content if isinstance(msg.content, str) else ""
            if hasattr(msg, "tool_call_id"):
                # tool result message
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = None
                if isinstance(payload, dict) and payload.get("proposal_path"):
                    staged_path = payload["proposal_path"]
            else:
                # Likely an AIMessage with the agent's reply text
                if content:
                    final_text = content

    invocation.final_text = final_text or "(no reply text produced)"
    invocation.staged_proposal_path = staged_path
    if staged_path:
        invocation.terminated_reason = "proposed"
    _write_audit(audit_path, invocation)

    return DmAgentResult(
        operator_message=final_text or "I didn't catch that — could you rephrase?",
        staged_proposal_path=staged_path,
        invocation=invocation,
    )


# ─── helpers ────────────────────────────────────────────────────────

def _build_llm(model: str) -> Any:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=model, temperature=0.2, max_tokens=2048)


def _write_audit(audit_path: Path, invocation: DmAgentInvocation) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "invocation_id": invocation.invocation_id,
        "started_at": invocation.started_at,
        "operator_user_id": invocation.operator_user_id,
        "source_text": invocation.source_text[:500],
        "model_used": invocation.model_used,
        "iterations": invocation.iterations,
        "tool_calls": invocation.tool_calls,
        "final_text": invocation.final_text[:500],
        "staged_proposal_path": invocation.staged_proposal_path,
        "cost_usd": invocation.cost_usd,
        "terminated_reason": invocation.terminated_reason,
        "error": invocation.error,
    }
    with audit_path.open("a") as f:
        f.write(json.dumps(row) + "\n")
