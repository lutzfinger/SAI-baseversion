"""Batch runner: reads historical eval + approval records, scores them, writes ScoredTrajectories.

No new LLM calls — purely a structured read+transform over existing stores.
Analogous to Hermes's batch_runner.py, operating over SAI's offline human
signal history rather than live rollouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.eval.record import EvalRecord
from app.eval.storage import EvalRecordStore
from app.observability.run_store import RunStore
from app.rl.models import RewardSource, ScoredTrajectory, TrajectoryStep
from app.rl.reward import HumanRewardScorer
from app.rl.trajectory import RawTrajectory, ScoredTrajectoryStore, TrajectoryStore


@dataclass
class BatchRunSummary:
    total_scored: int = 0
    skipped_no_trajectory: int = 0
    skipped_abstain: int = 0
    by_workflow: dict[str, int] = field(default_factory=dict)
    reward_distribution: dict[str, int] = field(default_factory=dict)
    ran_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def report(self) -> str:
        lines = [
            f"BatchRun @ {self.ran_at.isoformat()}",
            f"  scored:              {self.total_scored}",
            f"  skipped (no traj):   {self.skipped_no_trajectory}",
            f"  skipped (abstain):   {self.skipped_abstain}",
        ]
        if self.by_workflow:
            lines.append("  by workflow:")
            for wf, n in sorted(self.by_workflow.items()):
                lines.append(f"    {wf}: {n}")
        if self.reward_distribution:
            lines.append("  reward distribution:")
            for bucket, n in sorted(self.reward_distribution.items()):
                lines.append(f"    {bucket}: {n}")
        return "\n".join(lines)


class BatchTrajectoryRunner:
    """
    For each workflow_id:
      1. Read all tasks from RunStore to enumerate task_ids
      2. Read EvalRecords for those tasks (optionally filter to is_ground_truth)
      3. Find the linked RawTrajectory via record_id or invocation_id
      4. Score via HumanRewardScorer (eval record → fallback to approval)
      5. Emit ScoredTrajectory → output_store
    """

    def __init__(
        self,
        *,
        eval_store: EvalRecordStore,
        trajectory_store: TrajectoryStore,
        run_store: RunStore,
        scorer: HumanRewardScorer,
    ) -> None:
        self._eval_store = eval_store
        self._trajectory_store = trajectory_store
        self._run_store = run_store
        self._scorer = scorer

    def run(
        self,
        *,
        workflow_ids: list[str],
        output_store: ScoredTrajectoryStore,
        since: datetime | None = None,
        only_ground_truth: bool = True,
    ) -> BatchRunSummary:
        summary = BatchRunSummary()

        for workflow_id in workflow_ids:
            task_ids = self._task_ids_for_workflow(workflow_id)
            count = 0

            for task_id in task_ids:
                records = self._eval_store.read_all(task_id)
                if since:
                    records = [r for r in records if r.decided_at >= since]
                if only_ground_truth:
                    records = [r for r in records if r.is_ground_truth]

                for record in records:
                    scored = self._score_record(record, workflow_id)
                    if scored is None:
                        summary.skipped_no_trajectory += 1
                        continue
                    if scored.reward_source == RewardSource.ABSTAIN:
                        summary.skipped_abstain += 1
                        continue
                    output_store.append(scored)
                    count += 1
                    bucket = _reward_bucket(scored.reward)
                    summary.reward_distribution[bucket] = (
                        summary.reward_distribution.get(bucket, 0) + 1
                    )

            summary.by_workflow[workflow_id] = count
            summary.total_scored += count

        return summary

    def _task_ids_for_workflow(self, workflow_id: str) -> list[str]:
        try:
            tasks = self._run_store.list_tasks(workflow_id=workflow_id, limit=10_000)
            return [t.task_id for t in tasks]
        except Exception:
            return []

    def _score_record(
        self, record: EvalRecord, workflow_id: str
    ) -> ScoredTrajectory | None:
        trajectory = self._find_trajectory(record, workflow_id)
        if trajectory is None:
            return None

        signal = self._scorer.score_from_eval_record(
            record, trajectory_id=trajectory.trajectory_id
        )

        # Fallback: try the approval table if eval record has no ground truth
        if signal.source == RewardSource.ABSTAIN and trajectory.run_id:
            signal = self._try_approval_signal(
                trajectory.run_id, trajectory.trajectory_id
            )

        steps = [
            TrajectoryStep(
                tool_name=s.tool_name,
                args=s.args,
                result=s.result,
                at=s.at,
                latency_ms=s.latency_ms,
                error=s.error,
            )
            for s in trajectory.steps
        ]

        return ScoredTrajectory(
            trajectory_id=trajectory.trajectory_id,
            invocation_id=trajectory.invocation_id,
            run_id=trajectory.run_id,
            record_id=record.record_id,
            workflow_id=workflow_id,
            task_id=trajectory.task_id,
            system_prompt=trajectory.system_prompt,
            user_message=trajectory.user_message,
            steps=steps,
            final_response=trajectory.final_response,
            model_used=trajectory.model_used,
            cost_usd=trajectory.cost_usd,
            started_at=trajectory.started_at,
            completed_at=trajectory.completed_at,
            terminated_reason=trajectory.terminated_reason,
            reward=signal.scalar,
            reward_source=signal.source,
            human_actor=signal.decided_by,
            scored_at=datetime.now(UTC),
        )

    def _find_trajectory(
        self, record: EvalRecord, workflow_id: str
    ) -> RawTrajectory | None:
        for t in self._trajectory_store.read_all(workflow_id):
            if t.record_id == record.record_id:
                return t
        return None

    def _try_approval_signal(self, run_id: str, trajectory_id: str):
        from app.rl.models import RewardSignal
        try:
            for req in self._run_store.list_approval_requests():
                if req.run_id == run_id and req.status.value != "pending":
                    return self._scorer.score_from_approval(
                        req, trajectory_id=trajectory_id
                    )
        except Exception:
            pass
        return RewardSignal(
            trajectory_id=trajectory_id,
            source=RewardSource.ABSTAIN,
            scalar=0.0,
            decided_at=datetime.now(UTC),
        )


def _reward_bucket(reward: float) -> str:
    if reward >= 1.0:
        return "+1.0"
    if reward >= 0.3:
        return "+0.3"
    if reward > 0:
        return "+low"
    if reward == 0.0:
        return "0.0"
    if reward >= -0.8:
        return "-0.8"
    return "-1.0"
