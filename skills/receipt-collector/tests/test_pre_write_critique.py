"""Tests for the pre-write sanity gate (lib/pre_write_critique.py) + its
integration into ad_hoc_decomposed.auto_execute_ad_hoc.

No network: the LLM is reached only through an injected claude_loop_fn,
and the daemon's gmail_search / forbes_latest / create_gmail_draft are
monkeypatched for the two integration tests.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib import ad_hoc_decomposed as ad  # noqa: E402
from lib.pre_write_critique import (  # noqa: E402
    DraftCritiqueVerdict,
    critique_draft,
    fail_closed_verdict,
    recipient_is_grounded,
)


def _ns(final_text: str):
    return types.SimpleNamespace(final_text=final_text)


# ── Test 1 — recipient grounding (deterministic) ──────────────────────


def test_recipient_grounding() -> None:
    cands = ["karin.finger@gmx.net", "other@x.com"]
    assert recipient_is_grounded("karin.finger@gmx.net", cands, "draft to karin") is True
    # mixed case + the same address still grounds
    assert recipient_is_grounded("Karin.Finger@GMX.net", cands, "draft to karin") is True
    # only present in the request text
    assert recipient_is_grounded("bob@example.com", [], "send it to bob@example.com") is True
    # in neither evidence nor request → not grounded
    assert recipient_is_grounded("stranger@nope.com", cands, "draft to karin") is False


# ── Test 2 — verdict schema + fail_closed helper ──────────────────────


def test_verdict_schema() -> None:
    with pytest.raises(Exception):  # pydantic rejects out-of-enum verdict
        DraftCritiqueVerdict(verdict="maybe", reason="x")
    v = fail_closed_verdict("boom", failed_checks=["client_timeout"])
    assert v.verdict == "failed"
    assert v.source == "degraded"
    assert v.failed_checks == ["client_timeout"]


# ── Test 3 — deterministic FAIL makes NO LLM call ─────────────────────


def test_deterministic_fail_no_llm() -> None:
    calls = {"n": 0}

    def stub_loop(**kwargs):
        calls["n"] += 1
        raise AssertionError("LLM must not be called when recipient is ungrounded")

    v = critique_draft(
        request_text="draft to karin",
        recipient_email="stranger@nope.com",
        draft_body="hi",
        candidate_emails=["karin.finger@gmx.net"],
        forbes_evidence=[],
        claude_loop_fn=stub_loop,
        overlay={},
    )
    assert v.verdict == "failed"
    assert v.failed_checks == ["recipient_not_grounded"]
    assert v.source == "deterministic"
    assert calls["n"] == 0


# ── Test 4 — LLM tier PASS and FAIL ───────────────────────────────────


def test_llm_tier_pass_and_fail() -> None:
    common = dict(
        request_text="draft a reply to karin about my latest forbes article",
        recipient_email="karin.finger@gmx.net",
        draft_body="Hi Karin, my latest piece covers X. <url>",
        candidate_emails=["karin.finger@gmx.net"],
        forbes_evidence=[{"title": "Real One", "url": "https://www.forbes.com/x"}],
        overlay={},
    )

    def stub_pass(**kwargs):
        return _ns('{"verdict":"passed","reason":"grounded","failed_checks":[]}')

    def stub_fail(**kwargs):
        return _ns('{"verdict":"failed","reason":"cites an article not in the list",'
                   '"failed_checks":["no_fabricated_article"]}')

    vp = critique_draft(claude_loop_fn=stub_pass, **common)
    assert vp.verdict == "passed"
    assert vp.source.startswith("llm:")

    vf = critique_draft(claude_loop_fn=stub_fail, **common)
    assert vf.verdict == "failed"
    assert "no_fabricated_article" in vf.failed_checks
    assert vf.source.startswith("llm:")


# ── Test 5 — fail-closed on degraded LLM ──────────────────────────────


def test_fail_closed_on_degraded() -> None:
    common = dict(
        request_text="draft to karin",
        recipient_email="karin.finger@gmx.net",
        draft_body="hi",
        candidate_emails=["karin.finger@gmx.net"],
        forbes_evidence=[],
        overlay={},
    )

    def stub_raise(**kwargs):
        raise RuntimeError("api down")

    def stub_garbage(**kwargs):
        return _ns("this is not json")

    v1 = critique_draft(claude_loop_fn=stub_raise, **common)
    assert v1.verdict == "failed"
    assert v1.source == "degraded"

    v2 = critique_draft(claude_loop_fn=stub_garbage, **common)
    assert v2.verdict == "failed"
    assert v2.source == "degraded"


# ── integration helpers ───────────────────────────────────────────────

_REQUEST = "draft a Gmail reply to karin about my latest Forbes article"


def _make_loop(recipient_email: str, critique_verdict: str = "passed"):
    """A claude_loop_fn stub that dispatches by system_prompt:
    router → draft_email; draft-builder → a draft to `recipient_email`;
    critique → `critique_verdict`."""

    def loop(*, system_prompt, user_text, overlay, mode, use_web_search, model=None):
        if "Classify ONE operator task" in system_prompt:
            return _ns(json.dumps({
                "task_kind": "draft_email",
                "recipient_hint": "karin",
                "topic_hint": "latest forbes article",
                "calendar_day_hint": "", "origin_hint": "", "dest_hint": "",
            }))
        if "creating ONE Gmail DRAFT reply" in system_prompt:
            return _ns(json.dumps({
                "needs_clarification": False,
                "clarification_question": "",
                "recipient_email": recipient_email,
                "recipient_name": "Karin",
                "detected_language": "English",
                "article_title": "Real One",
                "article_url": "https://www.forbes.com/x",
                "subject": "Re: your note",
                "body": "Hi Karin, my latest Forbes piece covers X. https://www.forbes.com/x",
            }))
        if "INDEPENDENT reviewer running on a DIFFERENT model" in system_prompt:
            return _ns(json.dumps({
                "verdict": critique_verdict, "reason": "ok", "failed_checks": [],
            }))
        return _ns("{}")

    return loop


@pytest.fixture
def _patch_daemon_io(monkeypatch):
    """Monkeypatch the daemon's read-only tools + the draft creator."""
    monkeypatch.setattr(ad, "gmail_search", lambda *a, **k: [
        {"from_email": "karin.finger@gmx.net", "from_name": "Karin Finger",
         "subject": "hi", "snippet": "hallo Lutz", "date_iso": "2026-05-24", "days_ago": 3},
    ])
    monkeypatch.setattr(ad, "forbes_latest", lambda *a, **k: [
        {"title": "Real One", "date_iso": "2026-05-20",
         "url": "https://www.forbes.com/x", "summary_snippet": "..."},
    ])
    created: list[dict] = []
    monkeypatch.setattr(ad, "create_gmail_draft",
                        lambda **kw: (created.append(kw) or "draft123"))
    return created


# ── Test 6 — integration: ungrounded draft is HELD ────────────────────


def test_integration_ungrounded_held(_patch_daemon_io) -> None:
    created = _patch_daemon_io
    result = ad.auto_execute_ad_hoc(
        text=_REQUEST, overlay={},
        claude_loop_fn=_make_loop("wrong.person@nope.com", critique_verdict="passed"),
    )
    assert result["did_write"] is False
    assert result["status_label"] == "SAI/proposal"
    assert len(created) == 0  # the gate blocked the write
    assert "Held the draft" in result["reply_text"]


# ── Test 7 — integration: grounded draft proceeds ─────────────────────


def test_integration_grounded_proceeds(_patch_daemon_io) -> None:
    created = _patch_daemon_io
    result = ad.auto_execute_ad_hoc(
        text=_REQUEST, overlay={},
        claude_loop_fn=_make_loop("karin.finger@gmx.net", critique_verdict="passed"),
    )
    assert result["did_write"] is True
    assert result["status_label"] == "SAI/plan"
    assert len(created) == 1
    assert created[0]["to"] == "karin.finger@gmx.net"
    assert "Auto Execution, since low risk" in result["reply_text"]
