"""Trajectory capture and storage for RL training.

RawTrajectory is written alongside every AgentInvocation audit row —
it stores the full untruncated tool calls and system prompt that the
audit writer omits for space reasons.

ScoredTrajectoryStore is the output side: scored trajectories ready
for export to HuggingFace dataset format.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.rl.models import ScoredTrajectory, TrajectoryStep


class RawTrajectory(BaseModel):
    """Full untruncated agent run, suitable as an RL training input."""

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str = Field(default_factory=lambda: str(uuid4()))
    invocation_id: str
    run_id: str | None = None
    record_id: str | None = None   # set post-hoc by linker when EvalRecord is known
    workflow_id: str
    task_id: str | None = None

    system_prompt: str
    user_message: str
    steps: list[TrajectoryStep]
    final_response: str
    model_used: str
    cost_usd: float
    started_at: datetime
    completed_at: datetime
    terminated_reason: str  # end_turn | proposed | iteration_cap | error


class TrajectoryStore:
    """Append-only JSONL store for RawTrajectory, partitioned by workflow_id.

    Mirrors EvalRecordStore's layout exactly:
        <root>/<workflow_id>.jsonl  — one RawTrajectory per line
    """

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def _path(self, workflow_id: str) -> Path:
        safe = workflow_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.jsonl"

    def append(self, trajectory: RawTrajectory) -> None:
        path = self._path(trajectory.workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(trajectory.model_dump_json() + "\n")

    def read_all(self, workflow_id: str) -> list[RawTrajectory]:
        path = self._path(workflow_id)
        if not path.exists():
            return []
        out: list[RawTrajectory] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(RawTrajectory.model_validate_json(line))
        return out

    def find_by_invocation_id(
        self, workflow_id: str, invocation_id: str
    ) -> RawTrajectory | None:
        for t in self.read_all(workflow_id):
            if t.invocation_id == invocation_id:
                return t
        return None

    def link_record(
        self, workflow_id: str, invocation_id: str, record_id: str
    ) -> bool:
        """Set record_id on a trajectory matched by invocation_id. Returns True if found."""
        path = self._path(workflow_id)
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            t = RawTrajectory.model_validate_json(line)
            if t.invocation_id == invocation_id and t.record_id is None:
                t = t.model_copy(update={"record_id": record_id})
                updated = True
            new_lines.append(t.model_dump_json())
        if updated:
            path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return updated


class ScoredTrajectoryStore:
    """Append-only JSONL store for ScoredTrajectory, partitioned by workflow_id."""

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def _path(self, workflow_id: str) -> Path:
        safe = workflow_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}_scored.jsonl"

    def append(self, trajectory: ScoredTrajectory) -> None:
        path = self._path(trajectory.workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(trajectory.model_dump_json() + "\n")

    def read_all(self, workflow_id: str) -> list[ScoredTrajectory]:
        path = self._path(workflow_id)
        if not path.exists():
            return []
        out: list[ScoredTrajectory] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(ScoredTrajectory.model_validate_json(line))
        return out

    def read_all_workflows(self) -> list[ScoredTrajectory]:
        """Read every scored trajectory across all workflow files in the store."""
        out: list[ScoredTrajectory] = []
        for path in sorted(self.root.glob("*_scored.jsonl")):
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(ScoredTrajectory.model_validate_json(line))
        return out
