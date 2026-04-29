"""Hash-verifying loader for the merged runtime tree (Phase 1, handoff #2).

The overlay merge (Phase 0) writes a manifest with SHA-256 of every file. This
module reads that manifest and verifies file content against it before any
workflow / policy / prompt is parsed. Failure modes are typed exceptions:

- HashMismatchError      — file content does not match the manifest hash
- UnregisteredFileError  — file is on disk but not in the manifest
- MissingFileError       — file is in the manifest but not on disk
- UnverifiableModeError  — manifest is in `symlink` mode (strict only)

Three modes via `SAI_OVERLAY_VERIFY` env var:
  - strict (default): verification failures raise the typed exception
  - warn: failures are logged via an optional callback but loading continues
  - off: verification is skipped entirely (intended for tests + bootstrap)

Failures are reported through an optional `on_failure` callback so the audit
logger can record them without coupling this module to the audit module.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.runtime.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestCorruptError,
    ManifestNotFoundError,
)

logger = logging.getLogger(__name__)

VerifyMode = Literal["strict", "warn", "off"]
DEFAULT_MODE: VerifyMode = "strict"
ENV_VAR = "SAI_OVERLAY_VERIFY"


class OverlayVerifyError(Exception):
    """Base class for verification failures."""

    relpath: str

    def __init__(self, relpath: str, message: str) -> None:
        super().__init__(message)
        self.relpath = relpath


class HashMismatchError(OverlayVerifyError):
    """File content sha256 does not match the manifest entry."""

    def __init__(self, relpath: str, expected: str, actual: str) -> None:
        super().__init__(
            relpath,
            f"hash mismatch for {relpath}: expected {expected[:12]}, got {actual[:12]}",
        )
        self.expected = expected
        self.actual = actual


class UnregisteredFileError(OverlayVerifyError):
    """File exists on disk under the runtime root but is not in the manifest."""

    def __init__(self, relpath: str) -> None:
        super().__init__(
            relpath, f"unregistered file (not in manifest): {relpath}"
        )


class MissingFileError(OverlayVerifyError):
    """Manifest references a file that is not present on disk."""

    def __init__(self, relpath: str) -> None:
        super().__init__(
            relpath, f"manifest references missing file: {relpath}"
        )


class UnverifiableModeError(OverlayVerifyError):
    """Manifest mode is `symlink` — content can change without manifest update."""

    def __init__(self) -> None:
        super().__init__(
            "(manifest)",
            "manifest mode is `symlink`; symlink targets can change without "
            "manifest knowing. Re-merge with --mode copy to enable strict "
            "verification.",
        )


# Reuse manifest loading errors so callers can catch a single hierarchy
__all__ = [
    "DEFAULT_MODE",
    "ENV_VAR",
    "HashMismatchError",
    "ManifestCorruptError",
    "ManifestNotFoundError",
    "MissingFileError",
    "OverlayVerifyError",
    "UnregisteredFileError",
    "UnverifiableModeError",
    "Verifier",
    "VerificationFailureRecord",
    "VerifyMode",
    "build_verifier_for_runtime",
    "resolve_mode",
]


def build_verifier_for_runtime(
    runtime_root: Path | None,
    *,
    mode: VerifyMode | None = None,
    on_failure: "FailureCallback | None" = None,
) -> "Verifier | None":
    """Build a Verifier if `runtime_root` is set and a manifest exists there.

    Returns None when:
      - runtime_root is None (no overlay merge in effect)
      - mode resolves to "off"
      - the runtime root has no `.sai-overlay-manifest.json`

    Anything else (manifest corrupt, mode invalid, etc.) raises.
    """

    if runtime_root is None:
        return None
    resolved_mode = mode if mode is not None else resolve_mode()
    if resolved_mode == "off":
        return None
    manifest_path = runtime_root / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    return Verifier(
        runtime_root,
        mode=resolved_mode,
        on_failure=on_failure,
    )


FailureCallback = Callable[["VerificationFailureRecord"], None]


@dataclass(frozen=True)
class VerificationFailureRecord:
    """Structured record passed to `on_failure` callbacks (Phase 1.5)."""

    relpath: str
    error_type: str
    expected_sha256: str | None
    actual_sha256: str | None
    mode: VerifyMode
    manifest_mode: str
    timestamp: datetime


def resolve_mode(env: dict[str, str] | None = None) -> VerifyMode:
    """Read `SAI_OVERLAY_VERIFY` from env (or arg). Default: strict."""

    source = env if env is not None else os.environ
    raw = source.get(ENV_VAR, DEFAULT_MODE).strip().lower()
    if raw not in ("strict", "warn", "off"):
        raise ValueError(
            f"{ENV_VAR}={raw!r} is not one of 'strict', 'warn', 'off'"
        )
    return raw  # type: ignore[return-value]


# Files that are produced inside the runtime tree and don't belong to the manifest.
SKIP_RELPATHS = frozenset({MANIFEST_FILENAME})


def _iter_runtime_files(root: Path) -> Iterator[Path]:
    """Yield every regular file under root, skipping the manifest."""

    for path in sorted(root.rglob("*")):
        if path.is_dir() or path.is_symlink() and path.is_dir():
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SKIP_RELPATHS:
            continue
        yield path


def _hash_file(path: Path, *, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


class Verifier:
    """Verify files in the merged runtime tree against `.sai-overlay-manifest.json`.

    Construct once at process startup (the manifest is cached). Then call
    `verify(path)` before reading any workflow / policy / prompt file, or
    `verify_all()` once to fail fast on a tampered tree.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        manifest: Manifest | None = None,
        mode: VerifyMode | None = None,
        on_failure: FailureCallback | None = None,
    ) -> None:
        self.runtime_root = runtime_root.resolve()
        self.mode: VerifyMode = mode if mode is not None else resolve_mode()
        self._on_failure = on_failure
        if manifest is None:
            self.manifest = Manifest.load(self.runtime_root)
        else:
            self.manifest = manifest
        if self.manifest.mode == "symlink" and self.mode != "off":
            # Strict raises; warn logs and continues.
            self._raise(UnverifiableModeError())

    # ----- public API -----

    def relpath_for(self, path: Path) -> str:
        """Convert an absolute path under the runtime root to its manifest key."""

        return path.resolve().relative_to(self.runtime_root).as_posix()

    def verify(self, path: Path) -> None:
        """Verify a single file. Raises in strict mode; warns in warn mode."""

        if self.mode == "off":
            return
        rel = self.relpath_for(path)
        if rel in SKIP_RELPATHS:
            return

        entry = self.manifest.get(rel)
        if entry is None:
            self._raise(UnregisteredFileError(rel))
            return
        if not path.exists():
            self._raise(MissingFileError(rel))
            return
        actual = _hash_file(path)
        if actual != entry.sha256:
            self._raise(
                HashMismatchError(rel, expected=entry.sha256, actual=actual)
            )

    def verify_all(self) -> list[OverlayVerifyError]:
        """Walk the runtime tree, return every problem (does not raise).

        Use this at startup for fail-fast surfaces. The CLI subcommand
        `sai verify` wraps this and exits non-zero on any problem.
        """

        problems: list[OverlayVerifyError] = []
        if self.mode == "off":
            return problems

        # Manifest-mode check first: if symlink in strict, that's the only
        # interesting result.
        if self.manifest.mode == "symlink" and self.mode == "strict":
            err = UnverifiableModeError()
            self._record_failure(err)
            problems.append(err)
            return problems

        seen: set[str] = set()
        for path in _iter_runtime_files(self.runtime_root):
            rel = self.relpath_for(path)
            seen.add(rel)
            entry = self.manifest.get(rel)
            if entry is None:
                err_unreg = UnregisteredFileError(rel)
                self._record_failure(err_unreg)
                problems.append(err_unreg)
                continue
            actual = _hash_file(path)
            if actual != entry.sha256:
                err_hash = HashMismatchError(
                    rel, expected=entry.sha256, actual=actual
                )
                self._record_failure(err_hash)
                problems.append(err_hash)

        for rel in self.manifest.files:
            if rel not in seen:
                err_miss = MissingFileError(rel)
                self._record_failure(err_miss)
                problems.append(err_miss)

        return problems

    # ----- internal -----

    def _raise(self, err: OverlayVerifyError) -> None:
        self._record_failure(err)
        if self.mode == "strict":
            raise err
        # warn: log and continue
        logger.warning("overlay verify (%s): %s", self.mode, err)

    def _record_failure(self, err: OverlayVerifyError) -> None:
        if self._on_failure is None:
            return
        record = VerificationFailureRecord(
            relpath=err.relpath,
            error_type=type(err).__name__,
            expected_sha256=getattr(err, "expected", None),
            actual_sha256=getattr(err, "actual", None),
            mode=self.mode,
            manifest_mode=self.manifest.mode,
            timestamp=datetime.now(timezone.utc),
        )
        try:
            self._on_failure(record)
        except Exception:  # noqa: BLE001 — callback failures must not mask the error
            logger.exception(
                "overlay-verify on_failure callback raised for %s", err.relpath
            )
