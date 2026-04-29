"""Typed access to the overlay manifest written by `sai-overlay merge`.

The manifest at `<runtime>/.sai-overlay-manifest.json` is the source of truth
for which files belong to the runtime tree and what their content hashes are.
The hash-verifying loader (Phase 1) reads this file once at startup and
consults it before parsing any workflow / policy / prompt file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MANIFEST_FILENAME = ".sai-overlay-manifest.json"

ManifestMode = Literal["copy", "symlink"]


@dataclass(frozen=True)
class FileEntry:
    """One file's entry in the manifest."""

    sha256: str
    source: str  # "public" | "private"
    size_bytes: int


@dataclass(frozen=True)
class Manifest:
    """Parsed `.sai-overlay-manifest.json` with hash lookup by relpath."""

    schema_version: int
    mode: ManifestMode
    public_root: str
    private_root: str
    shadowed_count: int
    files: dict[str, FileEntry]
    runtime_root: Path

    @classmethod
    def load(cls, runtime_root: Path) -> "Manifest":
        """Read and parse the manifest from `<runtime_root>/.sai-overlay-manifest.json`."""

        manifest_path = runtime_root / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise ManifestNotFoundError(
                f"manifest not found at {manifest_path}; "
                f"run `sai-overlay merge` first"
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestCorruptError(
                f"manifest at {manifest_path} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise ManifestCorruptError("manifest root must be an object")

        files_raw = payload.get("files")
        if not isinstance(files_raw, dict):
            raise ManifestCorruptError("manifest missing 'files' object")

        files = {
            relpath: FileEntry(
                sha256=str(entry["sha256"]),
                source=str(entry["source"]),
                size_bytes=int(entry["size_bytes"]),
            )
            for relpath, entry in files_raw.items()
        }

        mode = payload.get("mode", "copy")
        if mode not in ("copy", "symlink"):
            raise ManifestCorruptError(f"unknown manifest mode: {mode!r}")

        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            mode=mode,
            public_root=str(payload.get("public_root", "")),
            private_root=str(payload.get("private_root", "")),
            shadowed_count=int(payload.get("shadowed_count", 0)),
            files=files,
            runtime_root=runtime_root,
        )

    def get(self, relpath: str) -> FileEntry | None:
        return self.files.get(relpath)


class ManifestNotFoundError(FileNotFoundError):
    """Raised when the runtime tree has no manifest at all."""


class ManifestCorruptError(ValueError):
    """Raised when the manifest exists but cannot be parsed."""
