from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def new_id(prefix: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}_{timestamp}_{uuid4().hex[:10]}"


def new_run_id(workflow_id: str) -> str:
    normalized = workflow_id.replace("-", "_")
    return new_id(f"run_{normalized}")
