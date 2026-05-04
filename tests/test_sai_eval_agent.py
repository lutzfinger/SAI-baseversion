"""Tests for the sai-eval agent runner (LangChain edition).

LLM (ChatAnthropic) is stubbed via LangChain's GenericFakeChatModel so
tests stay fast + offline. We verify:

  * No-tool-call response → operator_message is the LLM's text
  * Audit log row written per invocation
  * Missing ANTHROPIC_API_KEY → friendly fallback
  * agent.invoke crash → friendly fallback + audit error row
  * Tool surface metadata captured in audit row
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agents.sai_eval_agent import (
    LLM_ROLE,
    MAX_ITERATIONS,
    AgentResult,
    run_agent,
)
from app.llm.registry import get_model_for_role

# Per #24b: tests reference the model id by role lookup, never by literal id.
DEFAULT_MODEL = get_model_for_role(LLM_ROLE)


# ─── helpers ───────────────────────────────────────────────────────────


def _ctx_kwargs(tmp_path: Path, audit_path: Path) -> dict:
    return {
        "operator_user_id": "U999",
        "source_text": "tell me a joke",
        "proposed_dir": tmp_path / "proposed",
        "gmail_authenticator": MagicMock(),
        "audit_path": audit_path,
    }


def _fake_llm_returning(text: str):
    """Build a fake chat model that supports bind_tools (which
    GenericFakeChatModel does not). Returns one AIMessage and stops.
    """

    from typing import Any, List, Optional

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _ToolCapableFakeLLM(BaseChatModel):
        canned_text: str = ""

        @property
        def _llm_type(self) -> str:
            return "fake-tool-capable"

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(
            self,
            messages: List[BaseMessage],
            stop: Optional[List[str]] = None,
            run_manager: Optional[CallbackManagerForLLMRun] = None,
            **kwargs: Any,
        ) -> ChatResult:
            ai = AIMessage(content=self.canned_text)
            return ChatResult(generations=[ChatGeneration(message=ai)])

    return _ToolCapableFakeLLM(canned_text=text)


# ─── single-turn no-tool-call ─────────────────────────────────────────


class TestSingleTurnRefusal:
    def test_llm_text_passes_through_to_operator(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        llm = _fake_llm_returning(
            "That's outside what I do here — try `add rule: …`."
        )
        result = run_agent(llm=llm, **_ctx_kwargs(tmp_path, audit))
        assert isinstance(result, AgentResult)
        assert "outside what I do" in result.operator_message
        assert result.staged_proposal_path is None
        assert result.invocation.terminated_reason == "end_turn"

    def test_audit_row_captures_input_model_and_specs(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        llm = _fake_llm_returning("ok")
        run_agent(llm=llm, **_ctx_kwargs(tmp_path, audit))
        rows = audit.read_text().strip().splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["operator_user_id"] == "U999"
        assert row["source_text"].startswith("tell me a joke")
        assert row["model_used"] == DEFAULT_MODEL
        assert row["terminated_reason"] == "end_turn"
        # Tool surface metadata captured for audit consistency
        # (when the LLM model later changes, we know what tools were
        # available at the time of the invocation).
        names = {s["name"] for s in row["tool_specs"]}
        assert "search_gmail" in names
        assert "propose_classifier_rule" in names
        assert "propose_llm_example" in names


# ─── error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    def test_no_anthropic_key_returns_friendly(self, tmp_path, monkeypatch):
        audit = tmp_path / "audit.jsonl"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = run_agent(llm=None, **_ctx_kwargs(tmp_path, audit))
        assert "ANTHROPIC_API_KEY" in result.operator_message
        assert result.invocation.terminated_reason == "error"

    def test_agent_invoke_crash_returns_friendly(self, tmp_path):
        """If LangChain's agent.invoke raises, we still return a clean result."""

        audit = tmp_path / "audit.jsonl"

        class _CrashingLLM:
            """Looks enough like a chat model for create_agent to accept it,
            but blows up on the first invocation. We rely on create_agent
            to surface the error inside agent.invoke."""

            def __init__(self):
                pass

            def bind_tools(self, tools, **kw):
                raise RuntimeError("simulated LLM unavailable")

        result = run_agent(
            llm=_CrashingLLM(), **_ctx_kwargs(tmp_path, audit),
        )
        assert "Something went wrong" in result.operator_message
        assert result.invocation.terminated_reason == "error"
        assert result.invocation.error is not None


# ─── result shape ─────────────────────────────────────────────────────


class TestAgentResult:
    def test_result_has_invocation_audit(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        llm = _fake_llm_returning("ok")
        result = run_agent(llm=llm, **_ctx_kwargs(tmp_path, audit))
        assert result.invocation is not None
        assert result.invocation.invocation_id.startswith("agent_")
        assert result.invocation.iterations >= 1
        assert result.invocation.model_used == DEFAULT_MODEL
