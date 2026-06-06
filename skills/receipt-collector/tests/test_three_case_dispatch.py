"""Tests for the three-case (a/b/c) operator-facing dispatch taxonomy.

Pins:
  1. The Verdict enum still includes the legacy verdicts AND the new
     AD_HOC_CAPABLE one (case c).
  2. The CASE_FOR_VERDICT mapping is exhaustive.
  3. The rule-tier classifier still catches the explicit cost-compiler
     and eval-feedback phrases (no regression on the cheap path).
  4. The LLM-tier system prompt mentions all six verdicts AND the
     decision rules that distinguish (b) vs (c).
  5. EmailIntent serialises the new ad_hoc fields and round-trips,
     while still loading legacy intents (no intent_kind on disk →
     default 'cost_compiler').
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib import dispatch_agent, email_intents


# ─── 1. Verdict enum + CASE_FOR_VERDICT mapping ───────────────────────


def test_verdict_enum_includes_ad_hoc_capable() -> None:
    assert "AD_HOC_CAPABLE" in {v.value for v in dispatch_agent.Verdict}


def test_case_mapping_covers_every_verdict() -> None:
    mapped = set(dispatch_agent.CASE_FOR_VERDICT.keys())
    actual = set(dispatch_agent.Verdict)
    assert mapped == actual, f"missing: {actual - mapped}"


def test_case_mapping_assigns_known_workflows_to_case_a() -> None:
    m = dispatch_agent.CASE_FOR_VERDICT
    assert m[dispatch_agent.Verdict.COST_COMPILER] == "a"
    assert m[dispatch_agent.Verdict.EVAL_FEEDBACK] == "a"
    assert m[dispatch_agent.Verdict.GENERAL_QUERY] == "a"


def test_case_mapping_assigns_no_tools_to_case_b() -> None:
    assert dispatch_agent.CASE_FOR_VERDICT[
        dispatch_agent.Verdict.WORKFLOW_SUGGESTION
    ] == "b"


def test_case_mapping_assigns_has_tools_to_case_c() -> None:
    assert dispatch_agent.CASE_FOR_VERDICT[
        dispatch_agent.Verdict.AD_HOC_CAPABLE
    ] == "c"


# ─── 2. Rule-tier still works (no regression on cheap path) ────────────


@pytest.mark.parametrize(
    ("subject", "body", "want"),
    [
        ("Self-test 3: cost-compile INSEAD", "EUR trip", "COST_COMPILER"),
        ("compile receipts please", "for May", "COST_COMPILER"),
        ("wrong label on that email", "", "EVAL_FEEDBACK"),
        ("reclassify this", "", "EVAL_FEEDBACK"),
        # Matches the conservative IGNORE pattern `\bboarding\s+for\b`.
        ("LH401 boarding for SFO begins", "see attached", "IGNORE"),
    ],
)
def test_rules_classify_unchanged(subject: str, body: str, want: str) -> None:
    dispatch = dispatch_agent._rules_classify(subject, body)
    assert dispatch is not None
    assert dispatch.verdict.value == want


def test_rules_abstains_on_open_text() -> None:
    # The "idea: weekly content planner" subject should fall through to
    # the LLM tier — the rules tier must NOT pre-empt classification.
    assert dispatch_agent._rules_classify(
        "idea: weekly content planner",
        "Could we build a workflow that scans my Google Calendar?",
    ) is None


# ─── 3. LLM-tier prompt teaches the three cases ────────────────────────


def test_llm_prompt_mentions_every_verdict_name() -> None:
    prompt = dispatch_agent._LLM_SYSTEM_PROMPT
    for v in dispatch_agent.Verdict:
        assert v.value in prompt, f"prompt is missing verdict {v.value}"


def test_llm_prompt_teaches_case_decision() -> None:
    prompt = dispatch_agent._LLM_SYSTEM_PROMPT
    # The decision boundary between (b) and (c) must be explicit.
    assert "(case a)" in prompt
    assert "(case b)" in prompt
    assert "(case c)" in prompt
    assert "Side effects required" in prompt or "side effect" in prompt.lower()


# ─── 4. EmailIntent ad_hoc round-trip + legacy compatibility ──────────


def test_ad_hoc_intent_roundtrips() -> None:
    intent = email_intents.EmailIntent(
        thread_id="t1",
        status=email_intents.IntentStatus.AWAITING_APPROVAL,
        operator_email="hello@example.com",
        ts_opened="2026-05-26T00:00:00+00:00",
        ts_updated="2026-05-26T00:00:00+00:00",
        intent_kind="ad_hoc",
        ad_hoc_original_request="have I ever signed a partner agreement for cherry?",
        ad_hoc_last_proposal=(
            "TLDR: I don't have this as an approved workflow.\n\n"
            "STEPS:\n1. Search Gmail.\n2. Search Drive.\n\n"
            "Approve y/n"
        ),
    )
    d = intent.to_dict()
    assert d["intent_kind"] == "ad_hoc"
    assert "ad_hoc_original_request" in d
    assert "ad_hoc_last_proposal" in d
    restored = email_intents.EmailIntent.from_dict(d)
    assert restored.intent_kind == "ad_hoc"
    assert restored.ad_hoc_original_request == intent.ad_hoc_original_request
    assert restored.ad_hoc_last_proposal == intent.ad_hoc_last_proposal


def test_legacy_intent_without_kind_defaults_to_cost_compiler() -> None:
    """Existing on-disk intents have no `intent_kind` field. They must
    keep loading and default to the legacy cost_compiler routing."""
    legacy = {
        "thread_id": "t-legacy",
        "status": "AWAITING_APPROVAL",
        "operator_email": "hello@example.com",
        "ts_opened": "2026-05-01T00:00:00+00:00",
        "ts_updated": "2026-05-01T00:00:00+00:00",
        "trigger_subject": "compile receipts",
        "history": [],
        "agent_invocations": [],
        "staged_plan_path": "/tmp/plan.json",
        "final_invoice_id": None,
        "bot_sent_message_ids": [],
        "processed_operator_message_ids": [],
    }
    restored = email_intents.EmailIntent.from_dict(legacy)
    assert restored.intent_kind == "cost_compiler"
    assert restored.staged_plan_path == "/tmp/plan.json"
    assert restored.ad_hoc_original_request is None
    assert restored.ad_hoc_last_proposal is None


def test_open_intent_supports_ad_hoc_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        email_intents, "_state_root", lambda: tmp_path / "intents"
    )
    intent = email_intents.open_intent(
        thread_id="t-adhoc",
        operator_email="hello@example.com",
        trigger_subject="have I signed cherry?",
        first_text="have I ever signed a partner agreement for cherry?",
        intent_kind="ad_hoc",
        initial_status=email_intents.IntentStatus.AWAITING_APPROVAL,
    )
    assert intent.intent_kind == "ad_hoc"
    assert intent.status == email_intents.IntentStatus.AWAITING_APPROVAL
    # Round-trip from disk.
    reloaded = email_intents.load("t-adhoc")
    assert reloaded is not None
    assert reloaded.intent_kind == "ad_hoc"
    assert reloaded.status == email_intents.IntentStatus.AWAITING_APPROVAL


# ─── 5. general_assistant system prompts forbid markdown ──────────────


def test_general_assistant_prompts_forbid_markdown() -> None:
    from lib import general_assistant

    for prompt_name in (
        "_QUERY_SYSTEM_PROMPT",
        "_WORKFLOW_SUGGESTION_SYSTEM_PROMPT",
        "_AD_HOC_PROPOSAL_SYSTEM_PROMPT",
        "_AD_HOC_EXECUTION_SYSTEM_PROMPT",
    ):
        prompt = getattr(general_assistant, prompt_name)
        lower = prompt.lower()
        assert "plain-text" in lower or "plain text" in lower, (
            f"{prompt_name} does not mention plain-text output"
        )
        # The literal anti-markdown line is the binding cue we lock in.
        assert (
            "no `**bold**`" in prompt.lower()
            or "no **bold**" in prompt.lower()
        ), f"{prompt_name} does not forbid **bold**"


def test_workflow_suggestion_prompt_specifies_three_section_template() -> None:
    from lib import general_assistant

    prompt = general_assistant._WORKFLOW_SUGGESTION_SYSTEM_PROMPT
    assert "I don't have an approved workflow" in prompt
    assert "CLAUDE CODE PROMPT:" in prompt
    assert "principle #9" in prompt


def test_ad_hoc_proposal_prompt_specifies_template() -> None:
    from lib import general_assistant

    prompt = general_assistant._AD_HOC_PROPOSAL_SYSTEM_PROMPT
    assert "TLDR:" in prompt
    assert "STEPS:" in prompt
    assert "Approve y/n" in prompt
    assert "read-only" in prompt.lower()
