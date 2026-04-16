"""Append-only activity and golden-dataset storage for email-native SAI work."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SaiEmailActivityRecord(BaseModel):
    """One observable read/plan/execute activity for one email-native turn."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    activity_id: str
    thread_id: str
    message_id: str
    workflow_id: str
    run_id: str
    recorded_at: datetime
    activity_kind: str
    description: str
    approval_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaiEmailGoldenRecord(BaseModel):
    """One operator-approved email-native interaction captured for evaluation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    golden_id: str
    thread_id: str
    request_message_id: str
    workflow_id: str
    run_id: str
    approved_at: datetime
    approved_by: str
    request_kind: str
    response_mode: str
    short_response: str
    explanation: str
    activity_ids: list[str] = Field(default_factory=list)
    approval_request_id: str | None = None
    execution_status: Literal["completed", "needs_information", "failed"] = "completed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaiEmailActivityStore:
    """Write email-native activity rows into an append-only JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append_records(self, records: list[SaiEmailActivityRecord]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
                handle.write("\n")


class SaiEmailGoldenDatasetStore:
    """Write approved email-native examples into an append-only JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append_record(self, record: SaiEmailGoldenRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
            handle.write("\n")


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for records."""

    return datetime.now(UTC)
