"""Build DPO preference pairs from scored trajectories.

For each unique (system_prompt, user_message) group, pairs the
highest-reward trajectory against the lowest-reward one — producing
a (chosen, rejected) example for Direct Preference Optimization.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.rl.models import PreferencePair, RewardSource, ScoredTrajectory


class PreferencePairBuilder:
    """Groups ScoredTrajectories by prompt and emits DPO preference pairs."""

    def __init__(self, *, min_reward_gap: float = 0.5) -> None:
        # Only pair trajectories where reward(chosen) - reward(rejected) >= this.
        # Guards against near-identical rewards creating noisy training signal.
        self.min_reward_gap = min_reward_gap

    def build_pairs(
        self, trajectories: list[ScoredTrajectory]
    ) -> list[PreferencePair]:
        """Return all valid preference pairs from the given scored trajectories."""
        groups: dict[str, list[ScoredTrajectory]] = {}
        for t in trajectories:
            if t.reward_source == RewardSource.ABSTAIN:
                continue
            key = _group_key(t.system_prompt, t.user_message)
            groups.setdefault(key, []).append(t)

        pairs: list[PreferencePair] = []
        for group in groups.values():
            pairs.extend(self._pair_group(group))
        return pairs

    def _pair_group(
        self, group: list[ScoredTrajectory]
    ) -> list[PreferencePair]:
        positives = sorted(
            [t for t in group if t.reward > 0],
            key=lambda t: t.reward,
            reverse=True,
        )
        negatives = sorted(
            [t for t in group if t.reward < 0],
            key=lambda t: t.reward,
        )

        if not positives or not negatives:
            return []

        pairs: list[PreferencePair] = []
        for pos in positives:
            for neg in negatives:
                if pos.final_response == neg.final_response:
                    continue
                if (pos.reward - neg.reward) < self.min_reward_gap:
                    continue
                pairs.append(
                    PreferencePair(
                        workflow_id=pos.workflow_id,
                        system_prompt=pos.system_prompt,
                        prompt=pos.user_message,
                        chosen_response=pos.final_response,
                        rejected_response=neg.final_response,
                        chosen_reward=pos.reward,
                        rejected_reward=neg.reward,
                        chosen_trajectory_id=pos.trajectory_id,
                        rejected_trajectory_id=neg.trajectory_id,
                        created_at=datetime.now(UTC),
                    )
                )
        return pairs


def _group_key(system_prompt: str, user_message: str) -> str:
    h = hashlib.sha256((system_prompt + "\x00" + user_message).encode()).hexdigest()
    return h[:24]
