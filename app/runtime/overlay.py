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
import logging
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

LOGGER = logging.getLogger(__name__)

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
        # Runtime state lives at ~/Library/{Logs,Application Support}/SAI per
        # app/shared/config.py; any `logs/` folder in either repo tree is
        # legacy and must NOT be carried into the merged runtime — these can
        # be multi-GB (e.g. langgraph_checkpoints.sqlite backup) and dirty
        # the runtime tree's hash manifest.
        "logs",
        # Quarantined corpus-cleanup snapshots: lots of small files, not code.
        "quarantine",
    }
)
SKIP_FILE_NAMES = frozenset(
    {
        ".DS_Store",
        MANIFEST_FILENAME,
        # Per-repo classifier artifact, not framework or data.
        "split-classification.json",
    }
)
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


# Paths inside the runtime tree that --clean MUST NOT wipe.
#
# Background (2026-05-02): the runtime tree at ~/.sai-runtime mixes two
# kinds of content:
#   1. Source — copied from public + private on every merge (overwritten
#      anyway, safe to wipe with --clean before re-merging).
#   2. Runtime state — created BY the running system inside the runtime
#      tree (the venv built with `pip install -e .`, the cascade's
#      append-only logs, the disagreement queue, etc.). Not in either
#      source repo. Wiping these on --clean breaks the apply path (no
#      venv → can't run regression gate) and silently loses unsurfaced
#      disagreements + audit history.
#
# Each entry is a path relative to --out. If it exists, it survives
# --clean. The merge step still overwrites if the same relpath comes
# from public/private (safe — those are source files anyway).
PRESERVE_ON_CLEAN: tuple[str, ...] = (
    # Python virtual environment — created via `python -m venv .venv` +
    # `pip install -e .`. Wiping it forces a 30-60s rebuild on every
    # merge AND can fail (we hit a pip dep issue 2026-05-01).
    ".venv",
    # Eval system runtime state. The schemas (canaries.jsonl,
    # edge_cases.jsonl) come from the merge — those get overwritten.
    # Everything else here is written by the cascade or the Slack bot
    # at runtime and has no source-repo equivalent.
    "eval/disagreement_queue.jsonl",
    "eval/proposed",
    "eval/runs",
    "eval/local_cloud_comparisons.jsonl",
    "eval/local_cloud_disagreements.jsonl",
    "eval/local_cloud_stats.json",
    "eval/local_cloud_training_state.json",
    "eval/local_email_classification_alignments.jsonl",
    "eval/local_email_classification_alignment_addendum.md",
    "eval/local_llm_prompt_addendum.md",
    "eval/local_llm_prompt_addendum.json",
    "eval/local_operator_outcomes.jsonl",
    "eval/local_operator_outcome_failures.jsonl",
    "eval/sai_email_activities.jsonl",
    "eval/sai_email_golden_dataset.jsonl",
    "eval/granola_role_comparisons.jsonl",
    "eval/granola_role_disagreements.jsonl",
    "eval/cornell_ta_registry_state.json",
    "eval/langsmith_tracing_feedback_state.json",
    "eval/newsletter_subscription_memory.json",
    "eval/relationship_memory.json",
    "eval/quarantine",
    # Backup files left by the apply path before in-place edits. They
    # exist briefly during apply and get cleaned up on success; if a
    # crash leaves one orphaned, --clean shouldn't blow away the
    # rollback evidence.
    "prompts/email/keyword-classify.md.pre-apply-*",
)


def _is_preserved(rel_posix: str) -> bool:
    """True if `rel_posix` matches one of the PRESERVE_ON_CLEAN patterns
    (literal match OR ancestor-of-preserved-path OR glob).
    """

    import fnmatch

    for pattern in PRESERVE_ON_CLEAN:
        if rel_posix == pattern:
            return True
        # Treat preserved paths as preserved subtrees: if foo/bar is
        # preserved, foo/ is implicitly preserved (don't wipe the parent
        # dir while pulling out the child).
        if pattern.startswith(rel_posix + "/"):
            return True
        # Ancestors of preserved paths (so we don't wipe their parents)
        if rel_posix.startswith(pattern + "/"):
            return True
        # Glob match (e.g. ``*.pre-apply-*``)
        if "*" in pattern and fnmatch.fnmatch(rel_posix, pattern):
            return True
    return False


def _selective_clean(out: Path) -> int:
    """Remove files+dirs in `out` that aren't in PRESERVE_ON_CLEAN.

    Walks `out` top-down; for each entry decides based on its relpath.
    Returns the number of paths removed.
    """

    removed = 0
    if not out.exists():
        return 0
    # Walk the immediate children first so we can prune entire subtrees.
    for child in sorted(out.iterdir()):
        rel = child.relative_to(out).as_posix()
        if _is_preserved(rel):
            # Some preserved entries are directories — recurse into them
            # to wipe non-preserved children but keep preserved ones.
            if child.is_dir():
                removed += _selective_clean_subtree(out, child)
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
            removed += 1
        elif child.is_dir():
            shutil.rmtree(child)
            removed += 1
    return removed


def _selective_clean_subtree(root: Path, subtree: Path) -> int:
    removed = 0
    for child in sorted(subtree.iterdir()):
        rel = child.relative_to(root).as_posix()
        if _is_preserved(rel):
            if child.is_dir():
                removed += _selective_clean_subtree(root, child)
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
            removed += 1
        elif child.is_dir():
            shutil.rmtree(child)
            removed += 1
    return removed


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
        # Selective clean: preserve runtime state (.venv, cascade-written
        # eval files, etc.). See PRESERVE_ON_CLEAN above.
        _selective_clean(out)


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
    skipped_runtime_state: list[str] = []

    for rel in _iter_relpaths(public):
        relpath = rel.as_posix()
        # Don't overwrite preserved runtime state with source-tree
        # placeholders. The runtime is authoritative for these paths;
        # source repos may carry empty stubs as schema documentation.
        if _is_preserved(relpath):
            skipped_runtime_state.append(f"public:{relpath}")
            continue
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
        if _is_preserved(relpath):
            skipped_runtime_state.append(f"private:{relpath}")
            continue
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

    if skipped_runtime_state:
        LOGGER.info(
            "skipped %d runtime-state path(s) "
            "(see PRESERVE_ON_CLEAN; runtime is authoritative for these)",
            len(skipped_runtime_state),
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

    # ── PR 1 — non-fatal skill.yaml validation pre-check ────────────────
    # See ~/SAI/docs/PLAN-UNIFIED-SKILL-SYNC.md and
    # ~/Claude-Logs/code-plans/2026-05-27-sai-overlay-deploy-pr1-schema-validator.md.
    # Walks every skill.yaml in the merged tree; logs [validator] warnings
    # on failures. NEVER blocks the merge in this PR — behavior change to
    # fatal-on-invalid is deferred to PR 2 (deploy subcommand).
    try:
        _validate_merged_skills_nonfatal(out)
    except Exception as exc:  # noqa: BLE001 — pre-check must never break merge
        LOGGER.warning("[validator] pre-check itself errored (%s); skipped", exc)

    return result


def _validate_merged_skills_nonfatal(out: Path) -> None:
    """Walk merged tree's skills/<id>/skill.yaml; warn on invalid ones.

    Never raises. Never blocks the merge. PR 1 deliverable per
    ~/Claude-Logs/code-plans/2026-05-27-sai-overlay-deploy-pr1-schema-validator.md
    Step 11.

    Writes both LOGGER.warning (for structured logging) AND
    print(file=sys.stderr) (so the operator running `sai-overlay merge`
    actually sees the warnings — LOGGER is silent without a configured
    handler, matching the existing print()-to-stdout pattern in cli()).
    """

    # Defensive lazy import so a broken validator never breaks the merge.
    from app.skills.manifest_validator import validate_file  # noqa: PLC0415

    skills_dir = out / "skills"
    if not skills_dir.is_dir():
        return

    inspected = 0
    bad = 0
    for entry in sorted(skills_dir.iterdir()):
        if entry.name.startswith(("_", ".")):
            continue
        if not entry.is_dir():
            continue
        manifest_path = entry / "skill.yaml"
        if not manifest_path.is_file():
            continue
        inspected += 1
        try:
            _, report = validate_file(manifest_path, skill_dir=entry)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[validator] {entry.name}: pre-check raised ({exc}); ignoring",
                file=sys.stderr,
            )
            bad += 1
            continue
        if not report.ok:
            bad += 1
            head = report.errors[0]
            print(
                f"[validator] {entry.name}: invalid skill.yaml — "
                f"{head.rule}: {head.message} "
                f"(run `python -m app.skills.manifest_validator {manifest_path}` "
                f"for full report)",
                file=sys.stderr,
            )

    if inspected:
        print(
            f"[validator] inspected {inspected} skill.yaml file(s); "
            f"{bad} invalid (non-fatal)",
            file=sys.stderr,
        )


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


# ─── deploy (PR 2) — claude_code target ──────────────────────────────
#
# See ~/SAI/docs/PLAN-UNIFIED-SKILL-SYNC.md and
# ~/Claude-Logs/code-plans/2026-05-27-pr1a-schema-fits-reality-plus-pr2-deploy.md.
#
# Reads a merged skill's claude_code profile and copies its files to
# ~/.claude/skills/<id>/ (the Layer-1 user-installed location, which
# shadows the anthropic-skills plugin install). Approval-gated: --apply
# requires --approved-by <git-tag> that exists in the SAI repo.

DEPLOY_LOG_FILENAME = ".sai-deploy-log.jsonl"

# Filenames/prefixes never shipped to claude_code (private eval data, #16a).
_EVAL_FILENAMES = frozenset(
    {"canaries.jsonl", "edge_cases.jsonl", "workflow_regression.jsonl"}
)


def _is_private_eval_path(relpath: str) -> bool:
    p = relpath.replace("\\", "/")
    if p.startswith("eval/") or "/eval/" in p:
        return True
    return Path(p).name in _EVAL_FILENAMES


@dataclass
class DeployPlan:
    skill_id: str
    version: str
    target: str
    target_dir: Path
    files: list[str]            # relpaths to copy (post-filter)
    skipped_private: list[str]  # relpaths filtered out


def _tag_exists(sai_repo: Path, tag: str) -> bool:
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(sai_repo), "rev-parse", "--verify", f"refs/tags/{tag}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _tag_is_signed(sai_repo: Path, tag: str) -> bool:
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(sai_repo), "tag", "-v", tag],
        capture_output=True, text=True,
    )
    # `git tag -v` exits 0 only when a valid signature verifies.
    return r.returncode == 0


def plan_claude_code_deploy(
    *,
    skill_id: str,
    runtime_tree: Path,
    claude_code_root: Path,
) -> DeployPlan:
    """Compute (don't write) the claude_code deploy for one skill.

    Raises InputError on: missing skill dir, invalid manifest, no
    claude_code profile, target-not-supported.
    """
    # Lazy import — avoids a hard dep cycle and keeps merge import-light.
    from app.skills.manifest_validator import validate_file  # noqa: PLC0415

    skill_dir = runtime_tree / "skills" / skill_id
    manifest_path = skill_dir / "skill.yaml"
    if not manifest_path.is_file():
        raise InputError(f"no skill.yaml at {manifest_path}")

    manifest, report = validate_file(manifest_path, skill_dir=skill_dir)
    if manifest is None or not report.ok:
        first = report.errors[0] if report.errors else None
        detail = f" ({first.rule}: {first.message})" if first else ""
        raise InputError(f"skill {skill_id!r} manifest invalid{detail}")

    profiles = getattr(manifest, "profiles", None)
    cc = getattr(profiles, "claude_code", None) if profiles else None
    if cc is None or not cc.enabled:
        raise InputError(
            f"skill {skill_id!r} has no enabled claude_code profile "
            f"(deploy --target claude_code not supported for it)"
        )
    if "claude_code" not in cc.deploy_to:
        raise InputError(
            f"skill {skill_id!r} claude_code profile does not list "
            f"'claude_code' in deploy_to ({cc.deploy_to})"
        )

    subdir = cc.claude_code_subdir
    target_dir = (
        claude_code_root / subdir / skill_id if subdir
        else claude_code_root / skill_id
    )

    files: list[str] = []
    skipped: list[str] = []
    for rel in cc.files:
        if _is_private_eval_path(rel):
            skipped.append(rel)
        else:
            files.append(rel)

    return DeployPlan(
        skill_id=skill_id,
        version=manifest.identity.version,
        target="claude_code",
        target_dir=target_dir,
        files=files,
        skipped_private=skipped,
    )


def deploy_claude_code(
    *,
    skill_id: str,
    runtime_tree: Path,
    claude_code_root: Path,
    apply: bool,
    approved_by: Optional[str],
    sai_repo: Path,
    require_signed: bool = False,
) -> DeployPlan:
    """Plan, then (if apply) write the claude_code deploy for one skill.

    Fail-closed (#6, #21): --apply requires an existing approval tag.
    With ``require_signed=True`` the tag must be a verified GPG-signed tag
    (full #21 enforcement); default warns-but-proceeds on unsigned so the
    flow works before GPG is configured. Atomic writes (temp + rename).
    Appends a row to the deploy log.
    """
    plan = plan_claude_code_deploy(
        skill_id=skill_id,
        runtime_tree=runtime_tree,
        claude_code_root=claude_code_root,
    )

    if not apply:
        return plan  # dry-run: caller prints; nothing written.

    # ── Approval gate (#21) ──────────────────────────────────────────────
    if not approved_by:
        raise InputError(
            "deploy --apply requires --approved-by <git-tag> (no surface "
            "certifies its own deployment, #21)"
        )
    if not _tag_exists(sai_repo, approved_by):
        raise InputError(
            f"approval tag {approved_by!r} not found in {sai_repo} "
            f"(create it: git -C {sai_repo} tag -s {approved_by} -m '...')"
        )
    if not _tag_is_signed(sai_repo, approved_by):
        if require_signed:
            raise InputError(
                f"approval tag {approved_by!r} is not a verified signed tag and "
                f"--require-signed is set. Configure GPG signing (see "
                f"scripts/setup-skill-signing.sh) and re-tag with `git tag -s`."
            )
        print(
            f"[deploy] WARNING: approval tag {approved_by!r} is not a verified "
            f"signed tag; proceeding (pass --require-signed to enforce)",
            file=sys.stderr,
        )

    src_skill_dir = runtime_tree / "skills" / skill_id
    written: list[dict[str, str]] = []
    for rel in plan.files:
        src = src_skill_dir / rel
        if not src.is_file():
            raise InputError(f"manifest lists {rel!r} but it's missing at {src}")
        dst = plan.target_dir / rel
        if dst.exists():
            print(f"[deploy] overwriting {dst}", file=sys.stderr)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp, follow_symlinks=True)
        os.replace(tmp, dst)
        written.append({"relpath": rel, "sha256": _hash_file(dst)})

    # ── Deploy log (#7 drop-don't-delete; #23 hash record) ───────────────
    log_path = runtime_tree / DEPLOY_LOG_FILENAME
    row = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skill": skill_id,
        "version": plan.version,
        "target": "claude_code",
        "approved_by": approved_by,
        "target_dir": str(plan.target_dir),
        "files": written,
        "skipped_private": plan.skipped_private,
        "result": "ok",
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

    return plan


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
    p_merge.add_argument(
        "--verbose",
        action="store_true",
        help="list every shadowed file (default: just the count)",
    )

    p_verify = sub.add_parser("verify", help="re-hash a merged runtime")
    p_verify.add_argument("--runtime", required=True, type=Path)

    p_deploy = sub.add_parser(
        "deploy", help="deploy a skill's claude_code profile to ~/.claude/skills/"
    )
    p_deploy.add_argument("--skill", required=True, help="skill_id (workflow_id)")
    p_deploy.add_argument(
        "--target", choices=["claude_code"], default="claude_code",
        help="deploy target (only claude_code in PR 2)",
    )
    p_deploy.add_argument(
        "--runtime-tree", type=Path,
        default=Path(os.path.expanduser("~/.sai-runtime")),
    )
    p_deploy.add_argument(
        "--claude-code-root", type=Path,
        default=Path(os.path.expanduser("~/.claude/skills")),
        help="root to write claude_code skills into (override for tests)",
    )
    p_deploy.add_argument(
        "--sai-repo", type=Path,
        default=Path(__file__).resolve().parents[2].parent / "SAI",
        help="repo where the approval tag lives",
    )
    p_deploy.add_argument("--approved-by", default=None, help="approval git tag")
    p_deploy.add_argument(
        "--require-signed", action="store_true", default=False,
        help="refuse unless the approval tag is a verified GPG-signed tag (#21)",
    )
    mode = p_deploy.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true", default=False)

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
        print(f"  shadowed: {r.shadowed_count} file(s) (private wins over public)")
        if args.verbose:
            for rel in r.shadowed_files:
                print(f"    private overrides public: {rel}")
        elif r.shadowed_count:
            print(f"    (pass --verbose to list them)")
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

    if args.cmd == "deploy":
        apply = bool(args.apply)  # --apply overrides the default --dry-run
        try:
            plan = deploy_claude_code(
                skill_id=args.skill,
                runtime_tree=args.runtime_tree,
                claude_code_root=args.claude_code_root,
                apply=apply,
                approved_by=args.approved_by,
                sai_repo=args.sai_repo,
                require_signed=args.require_signed,
            )
        except OverlayError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        verb = "deployed" if apply else "DRY-RUN — would deploy"
        print(f"{verb} {plan.skill_id} v{plan.version} -> {plan.target_dir}")
        for rel in plan.files:
            print(f"  {'wrote' if apply else 'would write'}: {rel}")
        for rel in plan.skipped_private:
            print(f"  skipped (private eval): {rel}")
        if not apply:
            print("  (dry-run; pass --apply --approved-by <tag> to write)")
        return 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
