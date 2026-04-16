"""Append-only evaluation dataset storage for starter email classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class EmailEvalRecord(BaseModel):
    """One starter email classification row kept for future benchmarking."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    recorded_at: datetime
    workflow_id: str
    run_id: str
    message_id: str
    thread_id: str | None = None
    subject: str
    from_email: str
    predicted_level1: str
    predicted_level2: str
    confidence: float
    reason: str


class EmailEvalDatasetStore:
    """Write starter email eval rows into an append-only JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append_record(self, record: EmailEvalRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
            handle.write("\n")


def utc_now() -> datetime:
    return datetime.now(UTC)
