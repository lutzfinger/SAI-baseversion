"""Tests for the proposal-intake primitive."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.skills import proposal_intake


def _stage(root: Path, workflow_id: str, thread_id: str, body: dict) -> Path:
    wf_dir = root / workflow_id
    wf_dir.mkdir(parents=True, exist_ok=True)
    p = wf_dir / f"{thread_id}.yaml"
    p.write_text(yaml.safe_dump({**body, "workflow_id": workflow_id, "thread_id": thread_id}))
    return p


def test_scan_returns_empty_on_missing_root(tmp_path):
    out = proposal_intake.scan_pending_proposals(root=tmp_path / "missing")
    assert out == []


def test_scan_finds_all_workflow_proposals(tmp_path):
    _stage(tmp_path, "wf-one", "t1", {"draft": {"to": "a@example.edu"}})
    _stage(tmp_path, "wf-two", "t2", {"draft": {"to": "b@example.edu"}})
    _stage(tmp_path, "wf-one", "t3", {"draft": {"to": "c@example.edu"}})

    out = proposal_intake.scan_pending_proposals(root=tmp_path)
    assert len(out) == 3
    by_thread = {p.thread_id: p for p in out}
    assert by_thread["t1"].workflow_id == "wf-one"
    assert by_thread["t2"].workflow_id == "wf-two"


def test_scan_filters_by_workflow_ids(tmp_path):
    _stage(tmp_path, "wf-one", "t1", {})
    _stage(tmp_path, "wf-two", "t2", {})
    out = proposal_intake.scan_pending_proposals(
        workflow_ids=["wf-one"], root=tmp_path,
    )
    assert {p.thread_id for p in out} == {"t1"}


def test_scan_skips_unparseable_files(tmp_path, caplog):
    wf_dir = tmp_path / "wf-bad"
    wf_dir.mkdir(parents=True)
    (wf_dir / "broken.yaml").write_text("this is not: : valid: yaml: : :\n[unclosed")
    out = proposal_intake.scan_pending_proposals(root=tmp_path)
    assert out == []


def test_load_proposal_returns_none_for_missing(tmp_path):
    assert proposal_intake.load_proposal(tmp_path / "nope.yaml") is None


def test_load_proposal_round_trip(tmp_path):
    p = _stage(tmp_path, "wf", "t1", {"draft": {"to": "x@example.edu"}})
    out = proposal_intake.load_proposal(p)
    assert out is not None
    assert out.workflow_id == "wf"
    assert out.thread_id == "t1"
    assert out.body["draft"]["to"] == "x@example.edu"


def test_summary_text_includes_key_fields(tmp_path):
    p = _stage(tmp_path, "wf", "t1", {
        "draft": {},
        "course_display_name": "Test 101",
        "from": "student@example.edu",
        "ta_emails": ["a@example.edu", "b@example.edu"],
    })
    out = proposal_intake.load_proposal(p)
    summary = out.summary_text()
    assert "Test 101" in summary
    assert "student@example.edu" in summary
    assert "2 TAs" in summary


def test_discard_removes_file(tmp_path):
    p = _stage(tmp_path, "wf", "t1", {})
    assert p.exists()
    assert proposal_intake.discard_proposal(p) is True
    assert not p.exists()


def test_discard_idempotent_on_missing(tmp_path):
    p = tmp_path / "nope.yaml"
    assert proposal_intake.discard_proposal(p) is True
