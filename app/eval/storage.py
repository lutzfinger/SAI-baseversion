"""Storage for EvalRecord (JSONL, append-only) and Preference (YAML per task).

Layout:

  <eval_dir>/<task_id>/records.jsonl       — one EvalRecord per line, append-only
  <eval_dir>/<task_id>/preferences.yaml    — list of Preference objects

JSONL was chosen for records because the central use cases are streaming append
during runtime and bulk read during graduation review. YAML was chosen for
preferences because they are human-readable and human-edited (especially during
co-work approval flows).

Neither store enforces locking. The runtime should ensure single-writer-per-task
or wrap callers in a lock. Tests use tmp_path so concurrent test runs are isolated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from app.eval.preference import Preference
from app.eval.record import EvalRecord


class EvalRecordStore:
    """Append-only JSONL store for EvalRecord, partitioned by task_id."""

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def _path_for_task(self, task_id: str) -> Path:
        return self.root / task_id / "records.jsonl"

    def append(self, record: EvalRecord) -> None:
        """Append one record to the task's JSONL file. Creates dirs as needed."""

        path = self._path_for_task(record.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # mode="a" is the only write mode that's safe to interleave between
        # processes on POSIX for short writes (≤ PIPE_BUF). Pydantic JSON keeps
        # it on one line.
        line = record.model_dump_json()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self, task_id: str) -> list[EvalRecord]:
        """Read every record for a task. Returns [] if no records exist."""

        path = self._path_for_task(task_id)
        if not path.exists():
            return []
        records: list[EvalRecord] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                records.append(EvalRecord.model_validate_json(line))
        return records

    def find_by_input_id(self, task_id: str, input_id: str) -> list[EvalRecord]:
        """Return all records for a given task input id (latest reconciliation last)."""

        return [r for r in self.read_all(task_id) if r.input_id == input_id]


class PreferenceStore:
    """YAML-backed store for Preferences, one file per task_id.

    Preferences are read-mostly: they're consulted on every relevant runtime
    decision but updated only on human approval. The full file is rewritten on
    each save, so writes should be infrequent.
    """

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def _path_for_task(self, task_id: str) -> Path:
        return self.root / task_id / "preferences.yaml"

    def load(self, task_id: str) -> list[Preference]:
        """Return all preferences for a task. [] if file doesn't exist."""

        path = self._path_for_task(task_id)
        if not path.exists():
            return []
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items: list[dict[str, Any]] = raw.get("preferences", [])
        return [Preference.model_validate(item) for item in items]

    def save(self, task_id: str, preferences: list[Preference]) -> None:
        """Rewrite the preferences file for a task. Creates dirs as needed."""

        path = self._path_for_task(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "preferences": [_preference_to_yaml_dict(p) for p in preferences],
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def upsert(self, preference: Preference) -> None:
        """Replace-or-add a preference by preference_id; writes the whole file."""

        existing = self.load(preference.task_id)
        for index, item in enumerate(existing):
            if item.preference_id == preference.preference_id:
                existing[index] = preference
                break
        else:
            existing.append(preference)
        self.save(preference.task_id, existing)


def _preference_to_yaml_dict(model: BaseModel) -> dict[str, Any]:
    """Pydantic → plain dict suitable for YAML round-trip (datetimes → ISO strings)."""

    return json.loads(model.model_dump_json())
