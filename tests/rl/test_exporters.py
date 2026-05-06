"""Tests for HuggingFaceExporter — all three output formats."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.rl.exporters import HuggingFaceExporter
from app.rl.models import PreferencePair, RewardSource, ScoredTrajectory, TrajectoryStep

NOW = datetime.now(UTC)
EXPORTER = HuggingFaceExporter()


def _step() -> TrajectoryStep:
    return TrajectoryStep(tool_name="search", args="inbox", result="3 emails", at=NOW)


def _scored(reward: float = 1.0, source: RewardSource = RewardSource.APPROVAL_APPROVED) -> ScoredTrajectory:
    return ScoredTrajectory(
        trajectory_id="traj_001",
        invocation_id="inv_001",
        workflow_id="test-wf",
        system_prompt="You are SAI.",
        user_message="classify this email",
        steps=[_step()],
        final_response="newsletter",
        model_used="claude-haiku-4-5",
        cost_usd=0.001,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
        reward=reward,
        reward_source=source,
        scored_at=NOW,
    )


def _pair() -> PreferencePair:
    return PreferencePair(
        workflow_id="test-wf",
        system_prompt="You are SAI.",
        prompt="classify this email",
        chosen_response="newsletter",
        rejected_response="work",
        chosen_reward=1.0,
        rejected_reward=-1.0,
        chosen_trajectory_id="traj_a",
        rejected_trajectory_id="traj_b",
    )


class TestDPOExport:
    def test_writes_jsonl(self, tmp_path: Path):
        out = tmp_path / "dpo.jsonl"
        n = EXPORTER.export_dpo([_pair()], out)
        assert n == 1
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        assert rows[0]["prompt"] == "classify this email"
        assert rows[0]["chosen"] == "newsletter"
        assert rows[0]["rejected"] == "work"
        assert rows[0]["system"] == "You are SAI."

    def test_metadata_included(self, tmp_path: Path):
        out = tmp_path / "dpo.jsonl"
        EXPORTER.export_dpo([_pair()], out)
        row = json.loads(out.read_text().splitlines()[0])
        assert "pair_id" in row["metadata"]
        assert row["metadata"]["chosen_reward"] == 1.0

    def test_empty_input(self, tmp_path: Path):
        out = tmp_path / "dpo.jsonl"
        n = EXPORTER.export_dpo([], out)
        assert n == 0


class TestSFTExport:
    def test_writes_sharegpt_format(self, tmp_path: Path):
        out = tmp_path / "sft.jsonl"
        n = EXPORTER.export_sft([_scored(reward=1.0)], out)
        assert n == 1
        row = json.loads(out.read_text().splitlines()[0])
        assert row["system"] == "You are SAI."
        roles = [m["role"] for m in row["messages"]]
        assert roles == ["user", "tool", "assistant"]

    def test_min_reward_filter(self, tmp_path: Path):
        out = tmp_path / "sft.jsonl"
        n = EXPORTER.export_sft(
            [_scored(reward=1.0), _scored(reward=0.1)], out, min_reward=0.3
        )
        assert n == 1

    def test_tool_step_in_messages(self, tmp_path: Path):
        out = tmp_path / "sft.jsonl"
        EXPORTER.export_sft([_scored()], out)
        row = json.loads(out.read_text().splitlines()[0])
        tool_msg = next(m for m in row["messages"] if m["role"] == "tool")
        assert tool_msg["name"] == "search"
        assert tool_msg["content"] == "3 emails"


class TestScoredExport:
    def test_writes_scored_format(self, tmp_path: Path):
        out = tmp_path / "scored.jsonl"
        n = EXPORTER.export_scored([_scored()], out)
        assert n == 1
        row = json.loads(out.read_text().splitlines()[0])
        assert "prompt" in row
        assert row["reward"] == 1.0
        assert row["response"] == "newsletter"

    def test_abstain_excluded(self, tmp_path: Path):
        out = tmp_path / "scored.jsonl"
        n = EXPORTER.export_scored(
            [_scored(reward=0.0, source=RewardSource.ABSTAIN)], out
        )
        assert n == 0

    def test_prompt_includes_system(self, tmp_path: Path):
        out = tmp_path / "scored.jsonl"
        EXPORTER.export_scored([_scored()], out)
        row = json.loads(out.read_text().splitlines()[0])
        assert "You are SAI" in row["prompt"]

    def test_metadata_fields(self, tmp_path: Path):
        out = tmp_path / "scored.jsonl"
        EXPORTER.export_scored([_scored()], out)
        row = json.loads(out.read_text().splitlines()[0])
        assert row["metadata"]["workflow_id"] == "test-wf"
        assert row["metadata"]["n_steps"] == 1
