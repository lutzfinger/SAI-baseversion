"""
Audit log writer (atomic, base skill).

Writes JSON-lines to ~/Library/Logs/SAI/receipt-collector.jsonl by default.
Each step in the workflow calls log_event(...) so the run history is
reconstructable.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_LOG = Path.home() / "Library" / "Logs" / "SAI" / "receipt-collector.jsonl"


def log_event(event: str, payload: dict[str, Any], log_path: Path | None = None) -> None:
    p = log_path or DEFAULT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "ts": int(time.time()),
        "pid": os.getpid(),
        "event": event,
        **payload,
    }, sort_keys=False)
    with p.open("a") as f:
        f.write(line + "\n")
