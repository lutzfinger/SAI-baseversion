"""Index manifest — append-only record of (source_file, sha256, indexed_at).

Lets the indexer skip unchanged files on the next run AND lets the
operator audit which files have been ingested without spinning up
chromadb.

File path: ``<index_root>/manifest.json`` (sibling to ``chroma/``).
Schema is a flat dict keyed by source_path RELATIVE to content_root —
not an absolute path — so the manifest is portable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ManifestEntry(BaseModel):
    """One row in the index manifest."""

    model_config = ConfigDict(extra="forbid")

    sha256: str
    indexed_at: str  # ISO8601 UTC


class IndexManifest(BaseModel):
    """Typed wrapper around ``<index_root>/manifest.json``."""

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, ManifestEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> IndexManifest:
        if not path.exists():
            return cls(entries={})
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return cls(entries={})
        # Old-format manifest (single dict of {path: {hash, indexed_at}})
        # is the same shape we use today; just validate it.
        entries = {
            str(k): ManifestEntry(
                sha256=str(v.get("sha256") or v.get("hash") or ""),
                indexed_at=str(v.get("indexed_at") or ""),
            )
            for k, v in raw.items()
            if isinstance(v, dict)
        }
        return cls(entries=entries)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            rel: {"sha256": e.sha256, "indexed_at": e.indexed_at}
            for rel, e in sorted(self.entries.items())
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def is_unchanged(self, *, source_path: str, sha256: str) -> bool:
        entry = self.entries.get(source_path)
        return entry is not None and entry.sha256 == sha256

    def record(self, *, source_path: str, sha256: str) -> None:
        self.entries[source_path] = ManifestEntry(
            sha256=sha256,
            indexed_at=datetime.now(UTC).isoformat(),
        )

    def remove(self, source_path: str) -> None:
        self.entries.pop(source_path, None)
