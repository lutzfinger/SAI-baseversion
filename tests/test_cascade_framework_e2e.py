"""End-to-end framework regression test (per ship blocker #3).

Spins up the WHOLE cascade against the synthetic sample_echo_skill:
loads the manifest from disk, registers per-tier handlers, walks
each case in workflow_regression.jsonl, asserts the cascade
produces the expected outcome.

This catches drift in the framework's plumbing (manifest loader,
handler registration, cascade walk, audit log) — beyond what the
per-tier unit tests in test_cascade_runner.py cover.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.cascade import (
    CascadeStep,
    register_rules_handler,
    run_cascade,
)
from app.cascade import runner as cascade_runner
from app.skills.loader import load_skill_manifest

SAMPLE_SKILL_DIR = Path(__file__).parent.parent / "app" / "skills" / "sample_echo_skill"


@pytest.fixture(autouse=True)
def _clear_handlers():
    cascade_runner._RULES_HANDLERS.clear()
    yield
    cascade_runner._RULES_HANDLERS.clear()


def _keyword_classifier_handler(ctx, cfg: dict[str, Any]) -> CascadeStep:
    """Synthetic rules handler matching the sample skill's keyword_map."""
    text = (ctx.inputs.get("text") or "").lower()
    if not text:
        return CascadeStep(kind="no_op", reason="empty_input")
    keyword_map = cfg.get("keyword_map", {})
    for label, keywords in keyword_map.items():
        for keyword in keywords:
            if keyword.lower() in text:
                return CascadeStep(
                    kind="continue",
                    reason=f"keyword_match:{keyword}",
                    metadata={"classification": label, "matched_keyword": keyword},
                )
    return CascadeStep(kind="continue", reason="no_keyword_match")


def _stub_llm_handler(_label: str = "neutral"):
    """Stub for cloud_llm tier — pretends to call an LLM.

    Realistic handler shape: doesn't override a classification an
    earlier tier already set (rules tier wins on confident match).
    """
    def handler(ctx, _cfg):
        if ctx.accumulated.get("classification") is not None:
            return CascadeStep(kind="no_op", reason="earlier_tier_classified")
        return CascadeStep(
            kind="continue",
            reason="stub_llm_classified",
            metadata={"classification": _label, "from_llm": True},
        )
    return handler


def _load_workflow_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with (SAMPLE_SKILL_DIR / "workflow_regression.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


# ---------------------------------------------------------------------------
# 1. Manifest loads cleanly + has all required slots
# ---------------------------------------------------------------------------

def test_sample_skill_manifest_loads():
    manifest, report = load_skill_manifest(SAMPLE_SKILL_DIR)
    assert report.ok, f"sample skill manifest failed validation: {report.summary()}"
    assert manifest.identity.workflow_id == "sample-echo-classifier"
    assert len(manifest.cascade) >= 1
    # Every workflow has the three required eval kinds (#33 hard contract).
    eval_kinds = {ds.kind for ds in manifest.eval.datasets}
    assert {"canaries", "edge_cases", "workflow"}.issubset(eval_kinds)


# ---------------------------------------------------------------------------
# 2. Cascade walks end-to-end against each workflow_regression case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", _load_workflow_cases(), ids=lambda c: c["case_id"])
def test_workflow_regression_case(case: dict[str, Any], tmp_path: Path):
    """Each workflow_regression case must produce its expected_outcome."""
    manifest, report = load_skill_manifest(SAMPLE_SKILL_DIR)
    assert report.ok

    register_rules_handler(
        manifest.identity.workflow_id, "rules", _keyword_classifier_handler,
    )

    inputs = {"text": case["input"], "thread_id": case["case_id"]}
    extra = {
        "cloud_llm_handler_fn": _stub_llm_handler("neutral"),
        "proposed_dir": tmp_path / "proposals",
    }
    result = run_cascade(manifest=manifest, inputs=inputs, extra=extra)

    expected = case["expected_outcome"]
    classification = result.accumulated.get("classification")

    # Map expected_outcome strings to our cascade output assertions.
    if expected == "abstain":
        # Empty input → rules handler returns no_op → cascade short-circuits.
        assert result.final_verdict == "no_op", (
            f"{case['case_id']}: expected no_op, got {result.final_verdict}"
        )
    elif expected in ("positive", "negative"):
        assert classification == expected, (
            f"{case['case_id']}: classification {classification!r} != {expected!r}"
        )
    elif expected == "neutral_or_escalate":
        # Either rules says no_keyword_match (cascade continues to LLM stub
        # which returns "neutral") or the cascade escalates.
        assert classification in (None, "neutral") or result.final_verdict in (
            "escalate", "ready_to_propose",
        ), (
            f"{case['case_id']}: unexpected outcome — classification "
            f"{classification!r}, verdict {result.final_verdict!r}"
        )
    else:
        pytest.fail(f"unknown expected_outcome: {expected}")


# ---------------------------------------------------------------------------
# 3. Audit trail is populated for every cascade walk
# ---------------------------------------------------------------------------

def test_cascade_writes_audit_trail(tmp_path: Path):
    manifest, _ = load_skill_manifest(SAMPLE_SKILL_DIR)
    register_rules_handler(
        manifest.identity.workflow_id, "rules", _keyword_classifier_handler,
    )
    inputs = {"text": "this is great work", "thread_id": "audit_test"}
    result = run_cascade(
        manifest=manifest,
        inputs=inputs,
        extra={
            "cloud_llm_handler_fn": _stub_llm_handler("neutral"),
            "proposed_dir": tmp_path / "proposals",
        },
    )
    # audit_log on the result should list the tiers walked
    assert hasattr(result, "audit_log")
    assert len(result.audit_log) >= 1
    # At least the rules tier should have run
    tier_ids = [entry.get("tier") for entry in result.audit_log]
    assert "rules" in tier_ids


# ---------------------------------------------------------------------------
# 4. Handler isolation across workflows (different workflow_ids don't
#    share handler registrations)
# ---------------------------------------------------------------------------

def test_handler_scope_isolation():
    register_rules_handler("workflow_a", "rules", _keyword_classifier_handler)
    # workflow_b has no handler registered, so its rules tier escalates
    from types import SimpleNamespace
    m_b = SimpleNamespace(
        identity=SimpleNamespace(workflow_id="workflow_b"),
        cascade=[SimpleNamespace(tier_id="rules", kind="rules", config={})],
    )
    result = run_cascade(manifest=m_b, inputs={"text": "great"})
    assert result.final_verdict == "escalate"
    assert "no_rules_handler_registered" in result.final_reason


# ---------------------------------------------------------------------------
# 5. Manifest validation REFUSES skills missing required eval kinds
# ---------------------------------------------------------------------------

def test_manifest_loader_refuses_missing_eval(tmp_path: Path):
    """Per #33 hard contract — manifest must declare all 3 required eval kinds."""
    bad_skill = tmp_path / "bad_skill"
    bad_skill.mkdir()
    (bad_skill / "skill.yaml").write_text("""
schema_version: "1"
identity:
  workflow_id: bad-skill
  version: "0.1.0"
  owner: test
  description: missing eval datasets
trigger:
  kind: manual
  config: {}
cascade:
  - tier_id: rules
    kind: rules
    config: {}
    confidence_threshold: 0.85
    cost_cap_per_call_usd: 0.0
tools: []
eval:
  datasets:
    - kind: canaries
      path: canaries.jsonl
      min_count: 1
      fail_mode: hard_fail
feedback:
  channel: sai-eval
  patterns: [add_rule]
outputs:
  - name: out
    side_effect: none
    requires_approval: false
policy:
  approval_required: false
  cost_cap_per_invocation_usd: 0.05
  iteration_cap: 4
  daily_invocation_cap: 100
  audit_log_path: "~/tmp/bad.jsonl"
observability:
  metrics_emit: false
""", encoding="utf-8")
    (bad_skill / "canaries.jsonl").write_text(
        '{"case_id": "x", "input": "y", "expected": "z"}\n', encoding="utf-8",
    )
    _, report = load_skill_manifest(bad_skill)
    assert not report.ok, "manifest missing edge_cases + workflow eval should refuse"
