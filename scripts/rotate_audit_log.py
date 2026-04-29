"""Rotate the audit log by month. Archive previous months, keep current month live.

The audit logger (`app/observability/audit.py`) opens the file fresh for each
append, so renaming `audit.jsonl` mid-flight is safe — the next write creates a
new empty file. We use a rename-then-process pattern:

  1. Rename audit.jsonl -> audit.jsonl.rotating-{timestamp} (atomic).
     SAI's next write will recreate audit.jsonl empty.
  2. Read events from the renamed file, group by year-month.
  3. Append per-month groups to audit-YYYY-MM.jsonl.gz archives.
  4. Append current-month events (if any) back to audit.jsonl
     (which may now contain a few events written during the race window —
     append preserves both).
  5. Delete archives older than --retention-months.

There is a small race window between step 1 and step 4 where SAI could write to
the newly-empty audit.jsonl. Step 4 appends, which preserves those events.

Run nightly via launchd or cron. Idempotent.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AUDIT_PATH = Path("~/Library/Logs/SAI/audit.jsonl").expanduser()
DEFAULT_RETENTION_MONTHS = 6
ARCHIVE_GLOB = "audit-*.jsonl.gz"


def _ym(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _event_ym(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = event.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ym(dt)


def _scan_months(path: Path) -> dict[str, int]:
    """Return {ym: line_count} without loading every line into memory."""
    counts: dict[str, int] = defaultdict(int)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            ym = _event_ym(line)
            if ym is None:
                counts["__unparseable__"] += 1
            else:
                counts[ym] += 1
    return dict(counts)


def rotate(audit_path: Path, *, dry_run: bool, now: datetime) -> dict[str, object]:
    if not audit_path.exists():
        return {"status": "no-file", "reason": f"{audit_path} does not exist"}
    if audit_path.stat().st_size == 0:
        return {"status": "skipped", "reason": "audit.jsonl is empty"}

    months = _scan_months(audit_path)
    current_ym = _ym(now)
    previous_months = {ym: count for ym, count in months.items()
                       if ym != current_ym and ym != "__unparseable__"}

    if not previous_months:
        return {
            "status": "skipped",
            "reason": f"all events in current month {current_ym}",
            "months": months,
        }

    if dry_run:
        return {
            "status": "would-rotate",
            "current_month_kept": current_ym,
            "current_month_count": months.get(current_ym, 0),
            "would_archive": previous_months,
            "unparseable_lines": months.get("__unparseable__", 0),
        }

    # Step 1: rename live file
    rotating_path = audit_path.with_name(
        f"{audit_path.name}.rotating-{now.strftime('%Y%m%dT%H%M%SZ')}"
    )
    audit_path.rename(rotating_path)

    # Step 2-3: stream-split into per-month gzip archives
    archive_writers: dict[str, gzip.GzipFile] = {}
    current_month_lines: list[str] = []
    unparseable: list[str] = []
    try:
        with rotating_path.open("r", encoding="utf-8") as src:
            for line in src:
                ym = _event_ym(line)
                if ym is None:
                    unparseable.append(line)
                    continue
                if ym == current_ym:
                    current_month_lines.append(line)
                    continue
                if ym not in archive_writers:
                    archive_path = audit_path.with_name(f"audit-{ym}.jsonl.gz")
                    # Open in append-binary mode; gzip supports concatenated streams
                    archive_writers[ym] = gzip.open(archive_path, "ab", compresslevel=6)
                archive_writers[ym].write(line.encode("utf-8"))
    finally:
        for w in archive_writers.values():
            w.close()

    # Step 4: append current-month events back to live file (which may have
    # received new writes from SAI during the race window)
    if current_month_lines:
        with audit_path.open("a", encoding="utf-8") as live:
            live.writelines(current_month_lines)

    # Drop the .rotating file
    rotating_path.unlink()

    return {
        "status": "rotated",
        "archived_months": {ym: list(archive_writers.keys()).count(ym) for ym in archive_writers},
        "current_month_count": len(current_month_lines),
        "unparseable_lines": len(unparseable),
        "archives_written": [
            str(audit_path.with_name(f"audit-{ym}.jsonl.gz")) for ym in archive_writers
        ],
    }


def prune(
    audit_dir: Path,
    *,
    retention_months: int,
    dry_run: bool,
    now: datetime,
) -> list[dict[str, str]]:
    cutoff_ym = _ym(_subtract_months(now, retention_months))
    actions: list[dict[str, str]] = []
    for archive in sorted(audit_dir.glob(ARCHIVE_GLOB)):
        # Filename shape: audit-YYYY-MM.jsonl.gz
        body = archive.stem.removesuffix(".jsonl").removeprefix("audit-")
        last_ym = body.split("_to_")[-1]
        if last_ym < cutoff_ym:
            if dry_run:
                actions.append({"status": "would-delete", "path": str(archive),
                                "last_ym": last_ym, "cutoff_ym": cutoff_ym})
            else:
                archive.unlink()
                actions.append({"status": "deleted", "path": str(archive),
                                "last_ym": last_ym, "cutoff_ym": cutoff_ym})
    return actions


def _subtract_months(dt: datetime, months: int) -> datetime:
    year = dt.year
    month = dt.month - months
    while month <= 0:
        month += 12
        year -= 1
    return dt.replace(year=year, month=month, day=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH,
                        help=f"audit.jsonl path (default: {DEFAULT_AUDIT_PATH})")
    parser.add_argument("--retention-months", type=int, default=DEFAULT_RETENTION_MONTHS,
                        help=f"keep archives this many months (default: {DEFAULT_RETENTION_MONTHS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print actions without modifying files")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    audit_path = args.audit_path.expanduser()

    print(f"== audit log rotation ({'DRY RUN' if args.dry_run else 'LIVE'}) ==")
    print(f"audit_path:        {audit_path}")
    print(f"retention_months:  {args.retention_months}")
    print(f"now (UTC):         {now.isoformat()}")
    print()

    rotation = rotate(audit_path, dry_run=args.dry_run, now=now)
    print("rotation:")
    for k, v in rotation.items():
        print(f"  {k}: {v}")

    print()
    pruned = prune(
        audit_path.parent,
        retention_months=args.retention_months,
        dry_run=args.dry_run,
        now=now,
    )
    if pruned:
        for action in pruned:
            print(f"prune: {action}")
    else:
        print("prune: no archives older than retention window")

    if not args.dry_run and audit_path.exists():
        live_mb = audit_path.stat().st_size / 1024 / 1024
        print(f"\nlive audit.jsonl now: {live_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
