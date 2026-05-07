"""Tests for TrajectoryStore and ScoredTrajectoryStore."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.rl.models import RewardSource, ScoredTrajectory, TrajectoryStep
from app.rl.trajectory import RawTrajectory, ScoredTrajectoryStore, TrajectoryStore

NOW = datetime.now(UTC)


def _step(n: int = 1) -> TrajectoryStep:
    return TrajectoryStep(
        tool_name=f"tool_{n}",
        args=f"arg_{n}",
        result=f"result_{n}",
        at=NOW,
    )


def _raw(workflow_id: str = "test-wf", invocation_id: str = "inv_1") -> RawTrajectory:
    return RawTrajectory(
        invocation_id=invocation_id,
        workflow_id=workflow_id,
        system_prompt="You are SAI.",
        user_message="classify this email",
        steps=[_step(1), _step(2)],
        final_response="newsletter",
        model_used="claude-haiku-4-5",
        cost_usd=0.001,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
    )


def _scored(workflow_id: str = "test-wf") -> ScoredTrajectory:
    raw = _raw(workflow_id)
    return ScoredTrajectory(
        trajectory_id=raw.trajectory_id,
        invocation_id=raw.invocation_id,
        workflow_id=workflow_id,
        system_prompt=raw.system_prompt,
        user_message=raw.user_message,
        steps=raw.steps,
        final_response=raw.final_response,
        model_used=raw.model_used,
        cost_usd=raw.cost_usd,
        started_at=raw.started_at,
        completed_at=raw.completed_at,
        terminated_reason=raw.terminated_reason,
        reward=1.0,
        reward_source=RewardSource.APPROVAL_APPROVED,
        scored_at=NOW,
    )


class TestTrajectoryStore:
    def test_append_and_read(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        t = _raw()
        store.append(t)
        results = store.read_all("test-wf")
        assert len(results) == 1
        assert results[0].invocation_id == t.invocation_id
        assert results[0].user_message == "classify this email"
        assert len(results[0].steps) == 2

    def test_multiple_workflows_isolated(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        store.append(_raw("wf-a", "inv_a"))
        store.append(_raw("wf-b", "inv_b"))
        assert len(store.read_all("wf-a")) == 1
        assert len(store.read_all("wf-b")) == 1

    def test_read_empty_returns_empty_list(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        assert store.read_all("nonexistent") == []

    def test_find_by_invocation_id(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        store.append(_raw("wf", "inv_x"))
        store.append(_raw("wf", "inv_y"))
        found = store.find_by_invocation_id("wf", "inv_x")
        assert found is not None
        assert found.invocation_id == "inv_x"

    def test_find_missing_returns_none(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        assert store.find_by_invocation_id("wf", "missing") is None

    def test_link_record(self, tmp_path: Path):
        store = TrajectoryStore(root=tmp_path)
        t = _raw("wf", "inv_link")
        store.append(t)
        updated = store.link_record("wf", "inv_link", "rec_001")
        assert updated is True
        found = store.find_by_invocation_id("wf", "inv_link")
        assert found is not None
        assert found.record_id == "rec_001"

    def test_steps_roundtrip_untruncated(self, tmp_path: Path):
        long_arg = "x" * 5000
        t = _raw()
        t = t.model_copy(update={"steps": [
            TrajectoryStep(tool_name="big_tool", args=long_arg, result="ok", at=NOW)
        ]})
        store = TrajectoryStore(root=tmp_path)
        store.append(t)
        back = store.read_all("test-wf")[0]
        assert back.steps[0].args == long_arg


class TestScoredTrajectoryStore:
    def test_append_and_read(self, tmp_path: Path):
        store = ScoredTrajectoryStore(root=tmp_path)
        t = _scored()
        store.append(t)
        results = store.read_all("test-wf")
        assert len(results) == 1
        assert results[0].reward == 1.0
        assert results[0].reward_source == RewardSource.APPROVAL_APPROVED

    def test_read_all_workflows(self, tmp_path: Path):
        store = ScoredTrajectoryStore(root=tmp_path)
        store.append(_scored("wf-a"))
        store.append(_scored("wf-b"))
        all_t = store.read_all_workflows()
        assert len(all_t) == 2
