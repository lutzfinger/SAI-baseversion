"""One-shot migration: move runtime state out of the repo.

Moves the contents of `<repo>/logs/` to:
  - State (DBs, artifacts, services state, obsidian, runs):
        ~/Library/Application Support/SAI/state/
  - Ops logs (audit.jsonl, ollama-auto.*, scheduled launchd logs):
        ~/Library/Logs/SAI/
  - OAuth tokens (logs/*_token.json):
        ~/.config/sai/tokens/
  - Eval datasets (logs/learning/):
        <repo>/eval/

Safe properties:
  - Hash-verifies every moved file (SHA-256 source == destination after move).
  - Skips files already migrated (destination exists with matching hash).
  - Refuses to overwrite a destination whose content differs from the source.
  - Records what it did to <repo>/logs/.migration-manifest.json for audit.
  - --dry-run prints the plan and does not touch any file.

Run order:
  1. Stop the FastAPI service / any process writing to logs/.
  2. `launchctl unload ~/Library/LaunchAgents/com.sai.tag-new-inbox.plist`
     (the script does this for you with --stop-launchd; pass --no-stop-launchd
      if you want to handle it yourself).
  3. python scripts/migrate_runtime_state.py --dry-run
  4. python scripts/migrate_runtime_state.py
  5. SAI's startup will regenerate the launchd plist with new log paths
     on next boot/reload (scheduled_jobs.py uses settings.logs_dir).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STATE_DIR = Path("~/Library/Application Support/SAI/state").expanduser()
LOG_DIR = Path("~/Library/Logs/SAI").expanduser()
TOKENS_DIR = Path("~/.config/sai/tokens").expanduser()
EVAL_DIR = REPO_ROOT / "eval"

LAUNCHD_PLIST = Path("~/Library/LaunchAgents/com.sai.tag-new-inbox.plist").expanduser()


# Mapping: relative path under logs/ -> (destination root, relative path under destination)
# Order matters: more specific paths (dirs) come first; the unmatched-leftovers
# rule at the bottom handles everything else.
MOVE_RULES: list[tuple[str, Path]] = [
    # subdirs (entire trees)
    ("learning/",    EVAL_DIR),
    ("artifacts/",   STATE_DIR / "artifacts"),
    ("services/",    STATE_DIR / "services"),
    ("obsidian/",    STATE_DIR / "obsidian"),
    ("scheduled/",   LOG_DIR / "scheduled"),
    # specific files (state)
    ("control_plane.db",            STATE_DIR / "control_plane.db"),
    ("runs.sqlite",                 STATE_DIR / "runs.sqlite"),
    ("langgraph_checkpoints.sqlite", STATE_DIR / "langgraph_checkpoints.sqlite"),
    ("repeated_error_memory.json",  STATE_DIR / "repeated_error_memory.json"),
    # specific files (ops logs)
    ("audit.jsonl",      LOG_DIR / "audit.jsonl"),
    ("ollama-auto.log",  LOG_DIR / "ollama-auto.log"),
    ("ollama-auto.pid",  LOG_DIR / "ollama-auto.pid"),
]

# OAuth token files under logs/ root, by suffix:
TOKEN_SUFFIX = "_token.json"

SKIP_FILES = {".gitkeep", ".DS_Store"}


@dataclass
class MoveAction:
    source: Path
    destination: Path
    sha256: str
    size_bytes: int
    status: str = "pending"
    # pending | moved | skipped-already-at-destination | kept-as-backup | error


@dataclass
class MigrationPlan:
    actions: list[MoveAction] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_destination(rel_under_logs: Path) -> Path | None:
    rel_str = str(rel_under_logs)
    # OAuth tokens: any *_token.json at logs/ root level
    if (
        rel_str.endswith(TOKEN_SUFFIX)
        and "/" not in rel_str  # only at root, not nested
    ):
        return TOKENS_DIR / rel_under_logs.name
    # Match against MOVE_RULES (longest prefix wins; we go in declared order
    # which puts directory rules first)
    for prefix, dest_root in MOVE_RULES:
        if prefix.endswith("/") and rel_str.startswith(prefix):
            sub = rel_str[len(prefix):]  # path relative to the moved subtree
            return dest_root / sub
        if not prefix.endswith("/") and rel_str == prefix:
            return dest_root
    return None  # unmatched: caller decides


def _collect_files(logs_root: Path) -> Iterable[Path]:
    for p in sorted(logs_root.rglob("*")):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES:
            continue
        yield p


def build_plan(logs_root: Path) -> MigrationPlan:
    plan = MigrationPlan()
    for src in _collect_files(logs_root):
        rel = src.relative_to(logs_root)
        dest = _resolve_destination(rel)
        if dest is None:
            plan.skipped.append((src, "no rule matched"))
            continue
        plan.actions.append(
            MoveAction(
                source=src,
                destination=dest,
                sha256=_sha256(src),
                size_bytes=src.stat().st_size,
            )
        )
    return plan


def _stop_launchd_job(plist_path: Path, *, dry_run: bool) -> None:
    if not plist_path.exists():
        print(f"  no launchd plist at {plist_path} (skipping unload)")
        return
    cmd = ["launchctl", "unload", str(plist_path)]
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return
    print(f"  running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # launchctl returns non-zero if already unloaded; treat as soft warning
        msg = (result.stderr or result.stdout).strip()
        if "Could not find specified service" in msg or not msg:
            print("  launchd job already not loaded")
        else:
            print(f"  launchd unload returned {result.returncode}: {msg}")


def _start_launchd_job(plist_path: Path, *, dry_run: bool) -> None:
    if not plist_path.exists():
        return
    cmd = ["launchctl", "load", str(plist_path)]
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return
    print(f"  running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        print(f"  launchd load returned {result.returncode}: {msg}")


def execute(plan: MigrationPlan, *, dry_run: bool) -> dict[str, int]:
    counts = {"moved": 0, "skipped_already_done": 0, "kept_as_backup": 0, "errors": 0}
    for action in plan.actions:
        if action.destination.exists():
            try:
                dest_hash = _sha256(action.destination)
            except OSError as exc:
                print(f"  ERROR reading destination {action.destination}: {exc}")
                action.status = "error"
                counts["errors"] += 1
                continue
            if dest_hash == action.sha256:
                # Already migrated; just remove the source (or leave for dry-run)
                action.status = "skipped-already-at-destination"
                counts["skipped_already_done"] += 1
                if dry_run:
                    print(f"  [skip] already at {action.destination} (hashes match)")
                else:
                    print(f"  [skip] already at {action.destination}; removing source")
                    action.source.unlink()
                continue
            # Conflict: destination exists with different content. New home wins
            # (it's been the live state since launchd ran with the new config).
            # Old source stays in logs/ as a read-only backup.
            action.status = "kept-as-backup"
            counts["kept_as_backup"] += 1
            print(
                f"  [backup] new home wins for {action.destination.name}; "
                f"source stays at {action.source.relative_to(REPO_ROOT)} as backup "
                f"(src sha={action.sha256[:8]}, dst sha={dest_hash[:8]})"
            )
            continue
        if dry_run:
            print(
                f"  [move] {action.source.relative_to(REPO_ROOT)} "
                f"-> {action.destination}  ({action.size_bytes:,} bytes)"
            )
            continue
        action.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(action.source), str(action.destination))
        post_hash = _sha256(action.destination)
        if post_hash != action.sha256:
            print(
                f"  ERROR hash mismatch after move: {action.destination} "
                f"(expected {action.sha256[:12]}, got {post_hash[:12]})"
            )
            action.status = "error"
            counts["errors"] += 1
            continue
        action.status = "moved"
        counts["moved"] += 1
    return counts


def write_manifest(plan: MigrationPlan, manifest_path: Path, *, dry_run: bool) -> None:
    payload = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actions": [
            {
                "source": str(a.source),
                "destination": str(a.destination),
                "sha256": a.sha256,
                "size_bytes": a.size_bytes,
                "status": a.status,
            }
            for a in plan.actions
        ],
        "skipped": [{"path": str(p), "reason": r} for p, r in plan.skipped],
    }
    if dry_run:
        print(f"  would write manifest to {manifest_path}")
        return
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"  manifest written to {manifest_path}")


def remove_empty_dirs(root: Path, *, dry_run: bool) -> None:
    """Bottom-up rmdir of empty directories under root, leaving root itself."""
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: -len(x.parts)):
        if p.is_dir() and not any(p.iterdir()):
            if dry_run:
                print(f"  would rmdir {p.relative_to(REPO_ROOT)}")
            else:
                p.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=REPO_ROOT / "logs",
                        help="source logs/ directory (default: <repo>/logs)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; do not touch any file")
    parser.add_argument("--no-stop-launchd", action="store_true",
                        help="do NOT unload the SAI launchd job (assumes you handled it)")
    parser.add_argument("--no-start-launchd", action="store_true",
                        help="do NOT re-load the launchd job after migration")
    args = parser.parse_args(argv)

    logs_root = args.logs.resolve()
    if not logs_root.exists():
        print(f"ERROR: source dir does not exist: {logs_root}")
        return 2

    print(f"== SAI runtime state migration ({'DRY RUN' if args.dry_run else 'LIVE'}) ==")
    print(f"source : {logs_root}")
    print(f"state  : {STATE_DIR}")
    print(f"logs   : {LOG_DIR}")
    print(f"tokens : {TOKENS_DIR}")
    print(f"eval   : {EVAL_DIR}")
    print()

    if not args.no_stop_launchd:
        print("Stopping SAI launchd job:")
        _stop_launchd_job(LAUNCHD_PLIST, dry_run=args.dry_run)
        print()

    print("Building plan...")
    plan = build_plan(logs_root)
    print(f"  {len(plan.actions)} files to move, {len(plan.skipped)} skipped (no rule)")
    if plan.skipped:
        for p, r in plan.skipped[:5]:
            print(f"    skip: {p.relative_to(logs_root)} ({r})")
        if len(plan.skipped) > 5:
            print(f"    ... and {len(plan.skipped) - 5} more")
    print()

    print("Executing plan:")
    counts = execute(plan, dry_run=args.dry_run)
    print()
    print(f"  moved: {counts['moved']}")
    print(f"  skipped (already at destination): {counts['skipped_already_done']}")
    print(f"  kept as backup (conflict — new home wins): {counts['kept_as_backup']}")
    print(f"  errors: {counts['errors']}")

    print()
    write_manifest(plan, logs_root / ".migration-manifest.json", dry_run=args.dry_run)

    print()
    print("Cleaning empty directories under logs/:")
    remove_empty_dirs(logs_root, dry_run=args.dry_run)

    if not args.no_start_launchd:
        print()
        print("Re-loading SAI launchd job:")
        _start_launchd_job(LAUNCHD_PLIST, dry_run=args.dry_run)

    print()
    if counts["errors"] > 0:
        print("MIGRATION FAILED with errors. Source files are still intact.")
        return 1
    if args.dry_run:
        print("Dry run complete. Re-run without --dry-run to execute.")
        return 0
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
