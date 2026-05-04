"""Tests for the framework cascade runner (Path B per #33a)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.cascade import (
    CascadeContext,
    CascadeStep,
    register_rules_handler,
    run_cascade,
)
from app.cascade import runner as cascade_runner


def _manifest(workflow_id: str, *, cascade_specs: list[dict]):
    """Tiny stub mimicking SkillManifest shape that run_cascade reads.

    SkillManifest's real construction is heavy (full Pydantic
    validation, eval contract). We just need .identity.workflow_id
    + .cascade[].kind/.tier_id/.config for the runner.
    """
    cascade = [
        SimpleNamespace(
            tier_id=spec["tier_id"],
            kind=spec["kind"],
            config=spec.get("config", {}),
        )
        for spec in cascade_specs
    ]
    return SimpleNamespace(
        identity=SimpleNamespace(workflow_id=workflow_id),
        cascade=cascade,
    )


@pytest.fixture(autouse=True)
def _clear_handlers():
    cascade_runner._RULES_HANDLERS.clear()
    yield
    cascade_runner._RULES_HANDLERS.clear()


def test_unknown_tier_kind_escalates():
    m = _manifest("wf1", cascade_specs=[{"tier_id": "weird", "kind": "weird"}])
    result = run_cascade(manifest=m, inputs={})
    assert result.final_verdict == "escalate"
    assert "no_handler_for_kind" in result.final_reason


def test_unregistered_rules_handler_escalates():
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
    ])
    result = run_cascade(manifest=m, inputs={})
    assert result.final_verdict == "escalate"
    assert "no_rules_handler_registered" in result.final_reason


def test_registered_rules_handler_continues():
    def gate(ctx, cfg):
        return CascadeStep(kind="continue", reason="ok", metadata={"x": 1})

    register_rules_handler("wf1", "input_guards", gate)
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(manifest=m, inputs={"thread_id": "t1"}, extra={
        "proposed_dir": Path("/tmp/cascade_test_proposals"),
    })
    assert result.final_verdict == "ready_to_propose"
    assert result.accumulated.get("x") == 1


def test_no_op_short_circuits_cascade():
    def reject(ctx, cfg):
        return CascadeStep(kind="no_op", reason="bad_input")

    register_rules_handler("wf1", "input_guards", reject)
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
        {"tier_id": "classify", "kind": "cloud_llm"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(manifest=m, inputs={})
    assert result.final_verdict == "no_op"
    # Only one tier ran.
    assert len(result.audit_log) == 1


def test_escalate_short_circuits_cascade():
    def gate(ctx, cfg):
        return CascadeStep(kind="continue", reason="ok")

    def cls(ctx, cfg):
        return CascadeStep(kind="escalate", reason="exception_classified")

    register_rules_handler("wf1", "input_guards", gate)
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
        {"tier_id": "classify", "kind": "cloud_llm"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(
        manifest=m, inputs={},
        extra={"classify_handler_fn": cls},
    )
    assert result.final_verdict == "escalate"
    assert "exception_classified" in result.final_reason
    assert len(result.audit_log) == 2


def test_cloud_llm_handler_required_via_extra():
    """Skills supply per-tier LLM handlers in extra keyed by
    {tier_id}_handler_fn. Missing handler escalates."""
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "classify", "kind": "cloud_llm"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(manifest=m, inputs={})
    assert result.final_verdict == "escalate"
    assert "no_llm_handler_for_tier:classify" in result.final_reason


def test_human_tier_stages_proposal(tmp_path):
    def gate(ctx, cfg):
        return CascadeStep(kind="continue", reason="ok", metadata={
            "draft": {"to": "x@example.edu", "body": "hi"},
        })

    register_rules_handler("wf1", "input_guards", gate)
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(
        manifest=m, inputs={"thread_id": "thr_42"},
        extra={"proposed_dir": tmp_path / "proposed"},
    )
    assert result.final_verdict == "ready_to_propose"
    assert result.proposal_path is not None
    proposal_path = Path(result.proposal_path)
    assert proposal_path.exists()
    body = yaml.safe_load(proposal_path.read_text())
    assert body["workflow_id"] == "wf1"
    assert body["thread_id"] == "thr_42"
    assert body["draft"] == {"to": "x@example.edu", "body": "hi"}


def test_audit_log_records_each_tier_in_order(tmp_path):
    def g(ctx, cfg):
        return CascadeStep(kind="continue", reason="ok")

    def cls(ctx, cfg):
        return CascadeStep(kind="continue", reason="ok", metadata={
            "draft": {"to": "x@example.edu"},
        })

    register_rules_handler("wf1", "input_guards", g)
    register_rules_handler("wf1", "canonical_lookup", g)
    m = _manifest("wf1", cascade_specs=[
        {"tier_id": "input_guards", "kind": "rules"},
        {"tier_id": "canonical_lookup", "kind": "rules"},
        {"tier_id": "classify", "kind": "cloud_llm"},
        {"tier_id": "human", "kind": "human"},
    ])
    result = run_cascade(
        manifest=m, inputs={"thread_id": "tt"},
        extra={
            "classify_handler_fn": cls,
            "proposed_dir": tmp_path / "p",
        },
    )
    assert [row["tier"] for row in result.audit_log] == [
        "input_guards", "canonical_lookup", "classify", "human",
    ]
    assert result.final_verdict == "ready_to_propose"


def test_register_rules_handler_is_idempotent():
    def first(ctx, cfg):
        return CascadeStep(kind="no_op", reason="first")

    def second(ctx, cfg):
        return CascadeStep(kind="continue", reason="second")

    register_rules_handler("wf1", "g", first)
    register_rules_handler("wf1", "g", second)  # replaces

    m = _manifest("wf1", cascade_specs=[{"tier_id": "g", "kind": "rules"}])
    result = run_cascade(manifest=m, inputs={})
    # The second handler returned 'continue', not 'no_op'.
    assert result.final_verdict == "escalate"  # cascade_exhausted
    assert result.audit_log[0]["reason"] == "second"


def test_handlers_are_scoped_per_workflow_id():
    def for_one(ctx, cfg):
        return CascadeStep(kind="no_op", reason="from_one")

    register_rules_handler("wf-one", "g", for_one)
    m = _manifest("wf-two", cascade_specs=[{"tier_id": "g", "kind": "rules"}])
    result = run_cascade(manifest=m, inputs={})
    assert result.final_verdict == "escalate"
    assert "no_rules_handler_registered:wf-two/g" in result.final_reason
