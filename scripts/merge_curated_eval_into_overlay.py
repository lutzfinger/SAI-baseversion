"""Merge a reviewed eval-curation CSV into the operator overlay.

Reads the CSV produced by `curate_legacy_eval_dataset.py` (legacy
shape) OR the 2026-05-01 incremental-disagreement curation CSV
(`incremental_eval_review.csv`) after the operator has filled in
`keep_in_eval` and confirmed `human_l1`. For each yes-row, appends a
JSON line to the overlay JSONL in `EmailClassificationDatasetOverlayRow`
shape so the next regression / quality_check pass picks them up.

L2 intent is not human-certified for these rows (same convention as
`refresh_dataset_from_gmail_labels`: L1-only ground truth). L2 defaults
to `others`.

Idempotent: rows whose `message_id` already exists in the overlay are
skipped. Safe to re-run.

Usage:

  python -m scripts.merge_curated_eval_into_overlay \\
      --reviewed ~/Downloads/sai_incremental_eval_review_2026-05-01-reviewed.csv

  python -m scripts.merge_curated_eval_into_overlay \\
      --reviewed /tmp/curated.csv --dry-run    # show what would land
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from app.learning.email_classification_alignment import (
    EmailClassificationDatasetOverlayRow,
    EmailClassificationDatasetOverlayStore,
    load_email_classification_dataset_overlay,
)
from app.shared.config import get_settings
from app.workers.email_models import (
    LEVEL1_DISPLAY_NAMES,
    Level1Classification,
    Level2Intent,
)

VALID_L1: set[str] = set(get_args(Level1Classification))
DEFAULT_L2: Level2Intent = "others"
_DISPLAY_TO_BUCKET: dict[str, str] = {
    v.lower(): k for k, v in LEVEL1_DISPLAY_NAMES.items()
}
# Operator shorthand → canonical bucket. Singulars are common in
# free-form review notes; map them rather than reject the row.
_SINGULAR_TO_PLURAL: dict[str, str] = {
    "customer": "customers",
    "newsletter": "newsletters",
    "invoice": "invoices",
    "update": "updates",
    "friend": "friends",
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()
    overlay_path = settings.local_email_classification_dataset_overlay_path
    reviewed_path = Path(args.reviewed).expanduser()

    if not reviewed_path.exists():
        print(f"error: reviewed CSV not found: {reviewed_path}", file=sys.stderr)
        return 2

    existing_ids = {
        row.message_id
        for row in load_email_classification_dataset_overlay(overlay_path)
    }

    candidates: list[dict[str, Any]] = []
    skipped_blank = 0
    with reviewed_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            verdict = (raw.get("keep_in_eval") or "").strip().lower()
            # Operator may signal "keep" two ways:
            #   1. Set keep_in_eval = yes/y/keep/true/1, OR
            #   2. Fill the `human eval` column with an L1 value
            #      (the lighter-touch verdict format).
            human_eval = (raw.get("human eval") or "").strip()
            if verdict in {"yes", "y", "keep", "true", "1"} or human_eval:
                candidates.append(raw)
            else:
                # no / blank without human eval / unknown → skipped.
                skipped_blank += 1

    print(f"reviewed CSV: {reviewed_path}")
    print(f"rows marked yes: {len(candidates)}")
    print(f"rows skipped (blank/no/unknown): {skipped_blank}")
    print(f"existing overlay rows: {len(existing_ids)}")

    new_rows: list[EmailClassificationDatasetOverlayRow] = []
    skipped_dup = 0
    skipped_invalid = 0
    for raw in candidates:
        message_id = (raw.get("message_id") or "").strip()
        if not message_id:
            skipped_invalid += 1
            continue
        if message_id in existing_ids:
            skipped_dup += 1
            continue

        # L1 source priority:
        #   1. `human eval`  — operator's review verdict (highest precedence)
        #   2. `human_l1`    — system-suggested default operator confirmed
        #   3. `expected_level1_classification` — legacy column name
        raw_l1 = (
            (raw.get("human eval")
             or raw.get("human_l1")
             or raw.get("expected_level1_classification")
             or "")
            .strip()
            .lower()
        )
        # Tolerate display-name input ("Customers" → "customers") and
        # singular shorthand ("customer" → "customers").
        l1 = raw_l1
        if l1 not in VALID_L1:
            l1 = _DISPLAY_TO_BUCKET.get(l1, l1)
        if l1 not in VALID_L1:
            l1 = _SINGULAR_TO_PLURAL.get(l1, l1)
        if l1 not in VALID_L1:
            print(
                f"warning: skipping {message_id} — invalid L1 {raw_l1!r}; "
                f"valid: {sorted(VALID_L1)}",
                file=sys.stderr,
            )
            skipped_invalid += 1
            continue

        captured_at = _parse_dt(raw.get("captured_at")) or datetime.now(UTC)
        snippet = raw.get("snippet") or ""
        try:
            row = EmailClassificationDatasetOverlayRow(
                dataset_entry_id=f"curated_legacy::{message_id}::{l1}",
                captured_at=captured_at,
                requested_by="curate_legacy_eval_dataset",
                correction_reason=(
                    raw.get("operator_notes")
                    or "Curated from incremental-disagreement review 2026-05-01."
                ),
                message_id=message_id,
                thread_id=(raw.get("thread_id") or None) or None,
                from_email=raw["from_email"],
                from_name=(raw.get("from_name") or None) or None,
                to=[],
                cc=[],
                subject=raw.get("subject") or "",
                snippet=snippet,
                body_excerpt=(raw.get("body_excerpt") or snippet or ""),
                source_label=f"curated_disagreement::{message_id}",
                expected_level1_classification=l1,  # type: ignore[arg-type]
                expected_level2_intent=DEFAULT_L2,
                raw_level1_label=LEVEL1_DISPLAY_NAMES.get(l1, l1),
                raw_level2_label="Others",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warning: skipping {message_id} — {exc}", file=sys.stderr)
            skipped_invalid += 1
            continue
        new_rows.append(row)

    print(f"new rows ready to write: {len(new_rows)}")
    print(f"  duplicates skipped: {skipped_dup}")
    print(f"  invalid skipped:    {skipped_invalid}")
    print()

    if args.dry_run:
        print("--dry-run: not writing")
        for row in new_rows[:5]:
            print(f"  would add: {row.message_id} → "
                  f"{row.expected_level1_classification} ({row.subject[:50]})")
        if len(new_rows) > 5:
            print(f"  ... and {len(new_rows) - 5} more")
        return 0

    if not new_rows:
        print("nothing to write; overlay unchanged")
        return 0

    store = EmailClassificationDatasetOverlayStore(overlay_path)
    written = 0
    for row in new_rows:
        result = store.record_example(row=row)
        written += result.get("recorded", 0)
    print(f"wrote {written} rows → {overlay_path}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merge_curated_eval_into_overlay", description=__doc__
    )
    parser.add_argument("--reviewed", required=True,
                        help="path to the reviewed curation CSV")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be written; don't modify overlay")
    return parser.parse_args(argv)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
