"""Export scored trajectories and preference pairs to HuggingFace-compatible JSONL.

Three formats:
  DPO       — trl.DPOTrainer: {system, prompt, chosen, rejected}
  SFT       — ShareGPT messages: {system, messages: [user, tool*, assistant]}
  SCORED    — PPO/GRPO: {prompt, response, reward, metadata}

Usage:
    exporter = HuggingFaceExporter()
    n = exporter.export_dpo(pairs, Path("out/dpo_pairs.jsonl"))
    n = exporter.export_sft(trajectories, Path("out/sft.jsonl"), min_reward=0.3)
    n = exporter.export_scored(trajectories, Path("out/scored.jsonl"))
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.rl.models import PreferencePair, ScoredTrajectory, TrajectoryStep


class HuggingFaceExporter:

    def export_dpo(
        self,
        pairs: list[PreferencePair],
        output_path: Path,
    ) -> int:
        """Write DPO pairs in trl.DPOTrainer format. Returns rows written."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output_path.open("w", encoding="utf-8") as fh:
            for pair in pairs:
                row: dict[str, Any] = {
                    "system": pair.system_prompt,
                    "prompt": pair.prompt,
                    "chosen": pair.chosen_response,
                    "rejected": pair.rejected_response,
                    "metadata": {
                        "pair_id": pair.pair_id,
                        "workflow_id": pair.workflow_id,
                        "chosen_reward": pair.chosen_reward,
                        "rejected_reward": pair.rejected_reward,
                        "chosen_trajectory_id": pair.chosen_trajectory_id,
                        "rejected_trajectory_id": pair.rejected_trajectory_id,
                    },
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        return count

    def export_sft(
        self,
        trajectories: list[ScoredTrajectory],
        output_path: Path,
        *,
        min_reward: float = 0.3,
    ) -> int:
        """Write SFT examples in ShareGPT messages format. Returns rows written."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output_path.open("w", encoding="utf-8") as fh:
            for t in trajectories:
                if t.reward < min_reward:
                    continue
                messages = _to_sharegpt_messages(t)
                row: dict[str, Any] = {
                    "system": t.system_prompt,
                    "messages": messages,
                    "metadata": {
                        "trajectory_id": t.trajectory_id,
                        "workflow_id": t.workflow_id,
                        "reward": t.reward,
                        "reward_source": t.reward_source,
                        "model_used": t.model_used,
                        "cost_usd": t.cost_usd,
                    },
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        return count

    def export_scored(
        self,
        trajectories: list[ScoredTrajectory],
        output_path: Path,
    ) -> int:
        """Write scored examples for PPO/GRPO training. Returns rows written."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output_path.open("w", encoding="utf-8") as fh:
            for t in trajectories:
                if t.reward_source.value == "abstain":
                    continue
                row: dict[str, Any] = {
                    "prompt": _build_prompt(t),
                    "response": t.final_response,
                    "reward": t.reward,
                    "metadata": {
                        "trajectory_id": t.trajectory_id,
                        "workflow_id": t.workflow_id,
                        "reward_source": t.reward_source,
                        "human_actor": t.human_actor,
                        "model_used": t.model_used,
                        "cost_usd": t.cost_usd,
                        "terminated_reason": t.terminated_reason,
                        "n_steps": len(t.steps),
                    },
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        return count


def _to_sharegpt_messages(t: ScoredTrajectory) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": t.user_message}
    ]
    for step in t.steps:
        messages.append({
            "role": "tool",
            "name": step.tool_name,
            "content": step.result,
            "metadata": {"args": step.args, "error": step.error},
        })
    messages.append({"role": "assistant", "content": t.final_response})
    return messages


def _build_prompt(t: ScoredTrajectory) -> str:
    """Combine system prompt + user message into a single prompt string."""
    if t.system_prompt:
        return f"<|system|>\n{t.system_prompt}\n<|user|>\n{t.user_message}"
    return t.user_message
