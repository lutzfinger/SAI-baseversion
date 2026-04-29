"""Prune LangGraph checkpoints for completed runs older than N days, then VACUUM.

The LangGraph SQLite checkpointer (`checkpoints` and `writes` tables) keyed by
(thread_id, checkpoint_ns, checkpoint_id) has no native timestamp column. SAI's
runner names threads with an embedded ISO timestamp, e.g.

    run_email_triage_gmail_tagging_tagging_20260429T035439816971Z_<rand>

We use that pattern to identify thread age. Threads that don't match the
pattern are left alone (defensive — a future code change could rename).

Run nightly via launchd. Idempotent.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(
    "~/Library/Application Support/SAI/state/langgraph_checkpoints.sqlite"
).expanduser()
DEFAULT_RETENTION_DAYS = 7

# Matches the timestamp portion of SAI's thread_id naming convention:
# run_<workflow>_<step>_YYYYMMDDTHHMMSSffffffZ_<random>[ :message:<id>]
THREAD_TIMESTAMP_RE = re.compile(r"_(\d{8}T\d{6}\d*Z)_")
THREAD_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def _parse_thread_timestamp(thread_id: str) -> datetime | None:
    match = THREAD_TIMESTAMP_RE.search(thread_id)
    if not match:
        return None
    raw = match.group(1)
    # Strptime's %f handles up to 6 digits; LangGraph's runner uses up to 9.
    # Truncate the fractional-seconds part to 6 digits for parsing.
    if "T" in raw:
        date_part, time_part = raw.split("T", 1)
        seconds = time_part.rstrip("Z")
        if len(seconds) > 12:  # HHMMSS + 6+ frac digits
            seconds = seconds[:12]
        raw = f"{date_part}T{seconds}Z"
    try:
        return datetime.strptime(raw, THREAD_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _summarize(conn: sqlite3.Connection) -> dict[str, object]:
    cur = conn.cursor()
    checkpoints_count = cur.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    writes_count = cur.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    threads_count = cur.execute(
        "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
    ).fetchone()[0]
    return {
        "checkpoints": checkpoints_count,
        "writes": writes_count,
        "threads": threads_count,
    }


def prune(
    db_path: Path,
    *,
    retention_days: int,
    dry_run: bool,
    now: datetime,
    vacuum: bool,
) -> dict[str, object]:
    if not db_path.exists():
        return {"status": "no-db", "path": str(db_path)}

    cutoff = now - timedelta(days=retention_days)
    conn = sqlite3.connect(str(db_path))
    try:
        before = _summarize(conn)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
        all_threads: list[str] = [row[0] for row in cur.fetchall()]

        threads_to_delete: list[str] = []
        threads_unparseable = 0
        threads_kept = 0
        for thread_id in all_threads:
            ts = _parse_thread_timestamp(thread_id)
            if ts is None:
                threads_unparseable += 1
                continue
            if ts < cutoff:
                threads_to_delete.append(thread_id)
            else:
                threads_kept += 1

        result: dict[str, object] = {
            "before": before,
            "cutoff_utc": cutoff.isoformat(),
            "threads_total": len(all_threads),
            "threads_to_delete": len(threads_to_delete),
            "threads_kept": threads_kept,
            "threads_unparseable_skipped": threads_unparseable,
        }

        if not threads_to_delete:
            result["status"] = "no-action"
            return result

        if dry_run:
            result["status"] = "would-delete"
            result["sample"] = threads_to_delete[:5]
            return result

        # Delete in batches to keep parameter list manageable
        BATCH = 500
        deleted_checkpoints = 0
        deleted_writes = 0
        for i in range(0, len(threads_to_delete), BATCH):
            batch = threads_to_delete[i : i + BATCH]
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                f"DELETE FROM writes WHERE thread_id IN ({placeholders})", batch
            )
            deleted_writes += cur.rowcount
            cur.execute(
                f"DELETE FROM checkpoints WHERE thread_id IN ({placeholders})", batch
            )
            deleted_checkpoints += cur.rowcount
        conn.commit()

        result["status"] = "deleted"
        result["deleted_checkpoints"] = deleted_checkpoints
        result["deleted_writes"] = deleted_writes

        if vacuum:
            conn.execute("VACUUM")
            result["vacuumed"] = True

        result["after"] = _summarize(conn)
        return result
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH,
                        help=f"checkpoints DB (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"keep threads younger than this many days (default: {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted without changing the DB")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="skip VACUUM after delete (faster but does not reclaim disk)")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    db_path = args.db_path.expanduser()
    db_size_mb = db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0

    print(f"== langgraph checkpoint pruning ({'DRY RUN' if args.dry_run else 'LIVE'}) ==")
    print(f"db_path:        {db_path}")
    print(f"db_size_mb:     {db_size_mb:.1f}")
    print(f"retention_days: {args.retention_days}")
    print(f"now (UTC):      {now.isoformat()}")
    print()

    result = prune(
        db_path,
        retention_days=args.retention_days,
        dry_run=args.dry_run,
        now=now,
        vacuum=not args.no_vacuum,
    )
    for k, v in result.items():
        print(f"{k}: {v}")

    if not args.dry_run and db_path.exists():
        new_size_mb = db_path.stat().st_size / 1024 / 1024
        print(f"\ndb_size_mb_after: {new_size_mb:.1f}  (delta: {new_size_mb - db_size_mb:+.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
