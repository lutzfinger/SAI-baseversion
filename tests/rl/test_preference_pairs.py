"""Tests for PreferencePairBuilder."""

from __future__ import annotations

from datetime import UTC, datetime

from app.rl.models import RewardSource, ScoredTrajectory, TrajectoryStep
from app.rl.preference_pairs import PreferencePairBuilder

NOW = datetime.now(UTC)


def _traj(
    response: str,
    reward: float,
    reward_source: RewardSource = RewardSource.APPROVAL_APPROVED,
    user_message: str = "classify this email",
    system_prompt: str = "You are SAI.",
    workflow_id: str = "test-wf",
) -> ScoredTrajectory:
    return ScoredTrajectory(
        trajectory_id=f"traj_{response[:8]}_{reward}",
        invocation_id=f"inv_{response[:4]}",
        workflow_id=workflow_id,
        system_prompt=system_prompt,
        user_message=user_message,
        steps=[],
        final_response=response,
        model_used="claude-haiku-4-5",
        cost_usd=0.001,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
        reward=reward,
        reward_source=reward_source,
        scored_at=NOW,
    )


class TestPreferencePairBuilder:
    def test_basic_pair(self):
        builder = PreferencePairBuilder(min_reward_gap=0.5)
        trajectories = [
            _traj("Good reply.", reward=1.0),
            _traj("Bad reply.", reward=-1.0),
        ]
        pairs = builder.build_pairs(trajectories)
        assert len(pairs) == 1
        assert pairs[0].chosen_response == "Good reply."
        assert pairs[0].rejected_response == "Bad reply."
        assert pairs[0].chosen_reward == 1.0
        assert pairs[0].rejected_reward == -1.0

    def test_no_pairs_when_only_positives(self):
        builder = PreferencePairBuilder()
        pairs = builder.build_pairs([
            _traj("Good A.", reward=1.0),
            _traj("Good B.", reward=0.3),
        ])
        assert pairs == []

    def test_no_pairs_when_only_negatives(self):
        builder = PreferencePairBuilder()
        pairs = builder.build_pairs([
            _traj("Bad A.", reward=-1.0),
            _traj("Bad B.", reward=-0.8),
        ])
        assert pairs == []

    def test_gap_filter_excludes_close_rewards(self):
        builder = PreferencePairBuilder(min_reward_gap=0.5)
        pairs = builder.build_pairs([
            _traj("Decent.", reward=0.3),
            _traj("Slightly bad.", reward=-0.1),
        ])
        # gap = 0.4 < 0.5 → excluded
        assert pairs == []

    def test_abstain_excluded(self):
        builder = PreferencePairBuilder()
        pairs = builder.build_pairs([
            _traj("Good.", reward=1.0, reward_source=RewardSource.APPROVAL_APPROVED),
            _traj("No signal.", reward=0.0, reward_source=RewardSource.ABSTAIN),
            _traj("Bad.", reward=-1.0, reward_source=RewardSource.APPROVAL_DENIED),
        ])
        # Only good vs bad should pair; abstain is excluded
        assert len(pairs) == 1

    def test_different_prompts_dont_pair(self):
        builder = PreferencePairBuilder()
        pairs = builder.build_pairs([
            _traj("Good reply.", reward=1.0, user_message="email A"),
            _traj("Bad reply.", reward=-1.0, user_message="email B"),
        ])
        assert pairs == []

    def test_identical_responses_dont_pair(self):
        builder = PreferencePairBuilder()
        pairs = builder.build_pairs([
            _traj("Same reply.", reward=1.0),
            _traj("Same reply.", reward=-1.0),
        ])
        assert pairs == []

    def test_multiple_pairs_from_same_group(self):
        builder = PreferencePairBuilder(min_reward_gap=0.5)
        pairs = builder.build_pairs([
            _traj("Best reply.", reward=1.0),
            _traj("Good reply.", reward=0.3),
            _traj("Bad reply.", reward=-1.0),
        ])
        # best vs bad: gap 2.0 ✓; good vs bad: gap 1.3 ✓
        assert len(pairs) == 2
