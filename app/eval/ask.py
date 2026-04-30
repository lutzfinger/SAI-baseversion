"""Ask — one human-in-the-loop question and its lifecycle.

An Ask is created when:
  - HumanTier escalates a cascade abstain (kind=CLASSIFICATION)
  - Co-work or PreferenceRefiner proposes a preference (kind=PREFERENCE_*)
  - The AskOrchestrator decides to surface a borderline pending record

Asks live in `<eval_dir>/<task_id>/asks.jsonl`, one per line, append-only.
The reconciler scans `status=open` asks, polls the surface (Slack), and
updates them to `status=answered` (or `expired` after the window).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AskKind(StrEnum):
    """What an Ask is asking about."""

    CLASSIFICATION = "classification"           # cascade abstained, want a label
    PREFERENCE_PROPOSE = "preference_propose"   # approve a new preference?
    PREFERENCE_REFINE = "preference_refine"     # approve a refined preference?
    OTHER = "other"


class AskStatus(StrEnum):
    """Lifecycle state of an Ask."""

    OPEN = "open"            # posted; awaiting reply
    ANSWERED = "answered"    # human replied; reality propagated to records
    EXPIRED = "expired"      # window passed without reply
    CANCELLED = "cancelled"  # programmatically cancelled (e.g. record skipped first)


class Ask(BaseModel):
    """One Slack-mediated question + its lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    ask_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    kind: AskKind
    status: AskStatus = AskStatus.OPEN

    # The records this Ask corresponds to. Many-to-one is allowed: one Ask
    # can resolve multiple ambiguous EvalRecords from the same bucket.
    record_ids: list[str] = Field(default_factory=list)

    question_text: str
    options: list[str] = Field(default_factory=list)
    free_form_allowed: bool = True

    posted_to_channel: str
    posted_to_thread_ts: str | None = None       # the surface message id (Slack ts)

    posted_at: datetime
    expires_at: datetime | None = None

    answered_at: datetime | None = None
    answered_by: str | None = None
    answer: dict[str, Any] | None = None         # parsed reply

    metadata: dict[str, Any] = Field(default_factory=dict)


class AskStore:
    """JSONL append-only store for Asks, partitioned by task_id."""

    def __init__(self, *, root):
        self.root = root

    def _path_for_task(self, task_id: str):
        return self.root / task_id / "asks.jsonl"

    def append(self, ask: Ask) -> None:
        """Append one ask. Creates dirs as needed."""

        path = self._path_for_task(ask.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = ask.model_dump_json()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self, task_id: str) -> list[Ask]:
        """Return all asks for a task. May contain multiple records per ask_id
        when the lifecycle has been updated; the LATEST wins (callers fold)."""

        path = self._path_for_task(task_id)
        if not path.exists():
            return []
        asks: list[Ask] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                asks.append(Ask.model_validate_json(line))
        return asks

    def latest_state(self, task_id: str) -> dict[str, Ask]:
        """Fold the JSONL log: keep the most recent record per ask_id."""

        latest: dict[str, Ask] = {}
        for ask in self.read_all(task_id):
            existing = latest.get(ask.ask_id)
            if existing is None:
                latest[ask.ask_id] = ask
            else:
                # Append-only log: the later line is the newer version.
                # In practice posted_at is monotonic per ask, but file order
                # is the canonical truth either way.
                latest[ask.ask_id] = ask
        return latest

    def open_asks(self, task_id: str) -> list[Ask]:
        """Return only OPEN asks (post-fold), oldest first."""

        return [
            ask
            for ask in self.latest_state(task_id).values()
            if ask.status == AskStatus.OPEN
        ]
