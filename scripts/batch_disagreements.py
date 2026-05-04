"""Loop 2 v1 — disagreement triage (CSV-based, threshold-gated).

Per PRINCIPLES.md §16a Loop 2: when the disagreement queue accumulates
≥ DISAGREEMENT_BATCH_THRESHOLD unresolved rows, surface ONE batch ask
to the operator. v1 does this via a CSV in ~/Downloads/ + a desktop
notification. v2 (Phase 4) replaces this with a single Slack message
in #sai-eval.

Triggered hourly by launchd. No-ops below threshold.

Usage:
    sai eval batch-disagreements                        # check + maybe write CSV
    sai eval batch-disagreements --force                # write CSV regardless of count
    sai eval batch-disagreements --threshold 30         # override default 50
    sai eval batch-disagreements --output /tmp/x.csv    # custom path
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.eval.datasets import (
    DISAGREEMENT_BATCH_THRESHOLD,
    DisagreementRow,
)
from app.shared.config import get_settings

DEFAULT_QUEUE_PATH = Path("eval/disagreement_queue.jsonl")
DEFAULT_BATCH_SIZE = 15  # how many to surface per batch ask


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()
    queue_path = settings.root_dir / DEFAULT_QUEUE_PATH
    threshold = args.threshold

    if not queue_path.exists():
        print(f"queue empty (file not found): {queue_path}")
        return 0

    rows = [DisagreementRow.model_validate_json(l)
            for l in queue_path.read_text().splitlines() if l.strip()]
    unsurfaced = [r for r in rows if r.surfaced_in_batch_id is None]
    print(f"queue: {queue_path}")
    print(f"  total rows: {len(rows)}")
    print(f"  unsurfaced (eligible for batch): {len(unsurfaced)}")
    print(f"  threshold: {threshold}")

    if not args.force and len(unsurfaced) < threshold:
        print(f"\nbelow threshold ({len(unsurfaced)} < {threshold}); no batch")
        return 0

    # Pick the most informative subset using the same logic as the
    # original curation: drop noisy senders, dedupe per-sender, dedupe
    # against current dataset.
    selected = _select_batch(unsurfaced, cap=args.batch_size)
    if not selected:
        print("\nno batch candidates after filtering")
        return 0

    batch_id = f"batch-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    out_path = (
        Path(args.output).expanduser() if args.output
        else Path("~/Downloads").expanduser() / f"sai_disagreement_{batch_id}.csv"
    )
    _write_batch_csv(selected, path=out_path, batch_id=batch_id)
    print(f"\nwrote batch CSV ({len(selected)} rows) → {out_path}")

    # Mark selected rows as surfaced in this batch (so the next run
    # doesn't re-surface them).
    if not args.dry_run:
        _mark_surfaced(queue_path, selected_ids={r.disagreement_id for r in selected},
                       batch_id=batch_id)
        print(f"marked {len(selected)} queue rows as surfaced (batch_id={batch_id})")

    if not args.no_notify:
        _macos_notify(
            title="SAI eval batch ready",
            message=f"{len(selected)} disagreements ready for review in Downloads/.",
        )

    print(f"\nReview the CSV, fill in `human_l1` for keepers, save with -reviewed.csv")
    print(f"Then: sai eval resolve-batch --reviewed <path>")
    return 0


def _select_batch(
    rows: list[DisagreementRow], *, cap: int,
) -> list[DisagreementRow]:
    """Drop classifier-territory senders, cap per-sender to 2, prefer
    bucket diversity. Same logic as today's manual curation that
    produced the 25-row batch.
    """

    # Operator's own domains come from SAI_INTERNAL_DOMAINS (per #17 —
    # values are operator-specific, never hardcoded).
    import os
    internal_domains = {
        d.strip().lower() for d in os.environ.get("SAI_INTERNAL_DOMAINS", "").split(",")
        if d.strip()
    }

    def is_classifier_handled(r: DisagreementRow) -> bool:
        fe = (r.message.from_email or "").lower()
        if not fe:
            return True
        dom = fe.split("@")[-1] if "@" in fe else fe
        if dom in internal_domains:
            return True
        if fe.startswith(("no-reply@", "noreply@", "no-reply-",
                          "donotreply@", "do-not-reply@")):
            return True
        if r.message.list_unsubscribe:
            return True
        return False

    eligible = [r for r in rows if not is_classifier_handled(r)]

    by_sender: dict[str, list[DisagreementRow]] = defaultdict(list)
    for r in eligible:
        by_sender[(r.message.from_email or "").lower()].append(r)
    capped: list[DisagreementRow] = []
    for sender_rows in by_sender.values():
        capped.extend(sender_rows[:2])

    # Bucket-balanced round-robin so small buckets aren't crowded out.
    by_bucket: dict[str, list[DisagreementRow]] = defaultdict(list)
    for r in capped:
        by_bucket[r.cloud_prediction_l1].append(r)
    selected: list[DisagreementRow] = []
    while len(selected) < cap and any(by_bucket.values()):
        for bucket in list(by_bucket.keys()):
            if len(selected) >= cap: break
            if not by_bucket[bucket]: continue
            selected.append(by_bucket[bucket].pop(0))
    return selected


def _write_batch_csv(
    rows: list[DisagreementRow], *, path: Path, batch_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "keep_in_eval", "human_l1", "operator_notes",
            "cloud_l1", "cloud_reason",
            "local_l1", "local_reason",
            "rules_l1", "rules_conf",
            "from_email", "from_name", "subject", "snippet",
            "message_id", "thread_id", "captured_at",
            "disagreement_id", "batch_id",
        ])
        for r in rows:
            msg = r.message
            w.writerow([
                "",  # keep_in_eval — operator fills (yes/no)
                r.cloud_prediction_l1,  # human_l1 default = cloud's pick
                "",  # operator_notes
                r.cloud_prediction_l1,
                (r.cloud_prediction_reason or "")[:240],
                r.local_prediction_l1 or "",
                (r.local_prediction_reason or "")[:240],
                r.rules_prediction_l1 or "",
                r.rules_prediction_confidence or "",
                msg.from_email,
                msg.from_name or "",
                (msg.subject or "")[:200],
                (msg.snippet or "")[:300],
                r.message_id,
                msg.thread_id or "",
                r.captured_at.isoformat() if r.captured_at else "",
                r.disagreement_id,
                batch_id,
            ])


def _mark_surfaced(
    queue_path: Path, *, selected_ids: set[str], batch_id: str,
) -> None:
    """Rewrite queue file with surfaced_in_batch_id set for selected rows.

    Atomic via temp + rename.
    """

    rows = [DisagreementRow.model_validate_json(l)
            for l in queue_path.read_text().splitlines() if l.strip()]
    for r in rows:
        if r.disagreement_id in selected_ids and r.surfaced_in_batch_id is None:
            r.surfaced_in_batch_id = batch_id
    tmp = queue_path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(r.model_dump_json() for r in rows) + "\n")
    tmp.replace(queue_path)


def _macos_notify(*, title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass  # Notifications are best-effort; don't fail the run


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="batch_disagreements", description=__doc__
    )
    p.add_argument("--threshold", type=int, default=DISAGREEMENT_BATCH_THRESHOLD,
                   help=f"queue depth that triggers a batch (default {DISAGREEMENT_BATCH_THRESHOLD})")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help=f"max rows per batch ask (default {DEFAULT_BATCH_SIZE})")
    p.add_argument("--force", action="store_true",
                   help="write a batch even if queue is below threshold")
    p.add_argument("--output", default=None,
                   help="CSV path (default: ~/Downloads/sai_disagreement_<batch_id>.csv)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be written; don't mark queue rows as surfaced")
    p.add_argument("--no-notify", action="store_true",
                   help="skip macOS desktop notification")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
