"""Public/private overlay merge for SAI runtime.

Reads a public framework tree and a private overlay tree, produces a merged
output (typically ``~/.sai-runtime/``) with a manifest recording the source
(public/private) and SHA-256 of every file.

File-level override only: if private has ``workflows/x.yaml``, it replaces
public's ``workflows/x.yaml`` entirely. No per-key YAML merging.

The manifest (``.sai-overlay-manifest.json`` at the output root) is what the
hash-verifying loader (Phase 1) reads to confirm the runtime tree has not
been tampered with.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

MANIFEST_FILENAME = ".sai-overlay-manifest.json"
SCHEMA_VERSION = 1

Mode = Literal["copy", "symlink"]

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
)
SKIP_FILE_NAMES = frozenset({".DS_Store", MANIFEST_FILENAME})
SKIP_FILE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")


class OverlayError(Exception):
    """Base class for all overlay errors."""


class InputError(OverlayError):
    """Bad inputs: missing paths, conflicting flags, dangerous targets."""


class TypeConflictError(OverlayError):
    """Same relpath is a directory on one side and a file on the other."""


@dataclass(frozen=True)
class FileEntry:
    relpath: str
    sha256: str
    source: Literal["public", "private"]
    size_bytes: int


@dataclass
class MergeResult:
    out_path: Path
    files: dict[str, FileEntry]
    shadowed_files: list[str]
    mode: Mode
    public_root: Path
    private_root: Path

    @property
    def shadowed_count(self) -> int:
        return len(self.shadowed_files)

    @property
    def file_count(self) -> int:
        return len(self.files)


def _iter_relpaths(root: Path) -> Iterator[Path]:
    """Yield every regular-file path under ``root``, relative to root."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for fname in sorted(filenames):
            if fname in SKIP_FILE_NAMES:
                continue
            if fname.endswith(SKIP_FILE_SUFFIXES):
                continue
            full = Path(dirpath) / fname
            try:
                if not full.is_file():
                    continue
            except OSError:
                continue
            yield full.relative_to(root)


def _walk_dirs_and_files(root: Path) -> tuple[set[str], set[str]]:
    """Return ``(dirs, files)`` — relpaths under root, after skip filters."""
    dirs: set[str] = set()
    files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [n for n in dirnames if n not in SKIP_DIR_NAMES]
        d_path = Path(dirpath)
        if d_path != root:
            dirs.add(d_path.relative_to(root).as_posix())
        for n in filenames:
            if n in SKIP_FILE_NAMES or n.endswith(SKIP_FILE_SUFFIXES):
                continue
            files.add((d_path / n).relative_to(root).as_posix())
    return dirs, files


def _check_type_conflicts(public: Path, private: Path) -> None:
    pub_dirs, pub_files = _walk_dirs_and_files(public)
    prv_dirs, prv_files = _walk_dirs_and_files(private)
    conflicts = (pub_files & prv_dirs) | (pub_dirs & prv_files)
    if conflicts:
        first = sorted(conflicts)[0]
        raise TypeConflictError(
            f"type conflict at {first!r}: file on one side, directory on the other"
        )


def _hash_file(path: Path, *, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _place(src: Path, dst: Path, mode: Mode) -> None:
    if mode == "symlink":
        dst.symlink_to(src)
    else:
        shutil.copy2(src, dst, follow_symlinks=True)


def _validate_paths(public: Path, private: Path, out: Path, *, clean: bool) -> None:
    if not public.is_dir():
        raise InputError(f"--public must be an existing directory: {public}")
    if not private.is_dir():
        raise InputError(f"--private must be an existing directory: {private}")
    # Refuse if out is inside public or private (would create infinite-recursion
    # loops on subsequent merges).
    for src_name, src in (("--public", public), ("--private", private)):
        try:
            out.relative_to(src)
            raise InputError(f"--out cannot be inside {src_name}: {out}")
        except ValueError:
            pass
    if out.exists():
        if not clean:
            raise InputError(
                f"--out path already exists: {out} (pass --clean to overwrite)"
            )
        # Don't let --clean wipe obviously dangerous targets.
        dangerous = {Path("/").resolve(), Path.home().resolve()}
        if out in dangerous:
            raise InputError(f"refusing to --clean dangerous path: {out}")
        shutil.rmtree(out)


def merge(
    *,
    public: Path,
    private: Path,
    out: Path,
    mode: Mode = "copy",
    clean: bool = False,
) -> MergeResult:
    """Merge ``public`` + ``private`` into ``out`` and write the manifest."""
    public = public.resolve()
    private = private.resolve()
    out = out.resolve()

    _validate_paths(public, private, out, clean=clean)
    _check_type_conflicts(public, private)

    out.mkdir(parents=True, exist_ok=True)

    files: dict[str, FileEntry] = {}
    shadowed: list[str] = []

    for rel in _iter_relpaths(public):
        relpath = rel.as_posix()
        src = public / rel
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _place(src, dst, mode)
        files[relpath] = FileEntry(
            relpath=relpath,
            sha256=_hash_file(src),
            source="public",
            size_bytes=src.stat().st_size,
        )

    for rel in _iter_relpaths(private):
        relpath = rel.as_posix()
        src = private / rel
        dst = out / rel
        if relpath in files:
            shadowed.append(relpath)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        _place(src, dst, mode)
        files[relpath] = FileEntry(
            relpath=relpath,
            sha256=_hash_file(src),
            source="private",
            size_bytes=src.stat().st_size,
        )

    result = MergeResult(
        out_path=out,
        files=files,
        shadowed_files=sorted(shadowed),
        mode=mode,
        public_root=public,
        private_root=private,
    )
    _write_manifest(result)
    return result


def _write_manifest(result: MergeResult) -> None:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": result.mode,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "public_root": str(result.public_root),
        "private_root": str(result.private_root),
        "shadowed_count": result.shadowed_count,
        "shadowed_files": result.shadowed_files,
        "files": {
            rel: {
                "sha256": e.sha256,
                "source": e.source,
                "size_bytes": e.size_bytes,
            }
            for rel, e in sorted(result.files.items())
        },
    }
    manifest_path = result.out_path / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n")


def verify(runtime: Path) -> tuple[list[str], list[str], list[str]]:
    """Walk a merged runtime tree and compare against its manifest.

    Returns ``(mismatches, missing, unregistered)``: relpaths whose hash
    differs, relpaths in the manifest but absent from disk, and files on
    disk that the manifest does not list.

    Phase 1 will replace this lightweight checker with a hash-verifying
    loader that raises typed exceptions and writes audit rows.
    """
    runtime = runtime.resolve()
    manifest_path = runtime / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise InputError(f"no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text())

    expected: dict[str, str] = {
        rel: meta["sha256"] for rel, meta in manifest["files"].items()
    }
    on_disk: set[str] = set()
    mismatches: list[str] = []
    for rel in _iter_relpaths(runtime):
        relpath = rel.as_posix()
        on_disk.add(relpath)
        if relpath not in expected:
            continue
        actual = _hash_file(runtime / rel)
        if actual != expected[relpath]:
            mismatches.append(relpath)

    missing = sorted(set(expected) - on_disk)
    unregistered = sorted(on_disk - set(expected))
    return sorted(mismatches), missing, unregistered


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sai-overlay",
        description="Merge SAI public + private trees into a verified runtime.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="merge public + private into out")
    p_merge.add_argument("--public", required=True, type=Path)
    p_merge.add_argument("--private", required=True, type=Path)
    p_merge.add_argument("--out", required=True, type=Path)
    p_merge.add_argument("--mode", choices=["copy", "symlink"], default="copy")
    p_merge.add_argument(
        "--clean",
        action="store_true",
        help="remove --out path before merging if it exists",
    )

    p_verify = sub.add_parser("verify", help="re-hash a merged runtime")
    p_verify.add_argument("--runtime", required=True, type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "merge":
        try:
            r = merge(
                public=args.public,
                private=args.private,
                out=args.out,
                mode=args.mode,
                clean=args.clean,
            )
        except OverlayError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"merged {r.file_count} files -> {r.out_path}")
        print(f"  mode: {r.mode}")
        print(f"  shadowed_count: {r.shadowed_count}")
        for rel in r.shadowed_files:
            print(f"    private overrides public: {rel}")
        return 0

    if args.cmd == "verify":
        try:
            mism, miss, unreg = verify(args.runtime)
        except OverlayError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        problems = len(mism) + len(miss) + len(unreg)
        if problems == 0:
            print(f"verify ok: {args.runtime}")
            return 0
        print(f"verify FAILED: {problems} problem(s)", file=sys.stderr)
        for rel in mism:
            print(f"  hash mismatch: {rel}", file=sys.stderr)
        for rel in miss:
            print(f"  missing on disk: {rel}", file=sys.stderr)
        for rel in unreg:
            print(f"  unregistered file: {rel}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
