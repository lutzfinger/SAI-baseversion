"""Curate the legacy 435-row eval dataset down to rows that actually
test the LLM tiers.

Reasoning (operator request 2026-05-01):

  Rules-resolved rows add no eval value — rules are deterministic; if
  the keyword baseline matches, the answer is fixed and won't drift.
  We only need eval coverage for cases where the LLM tiers (local +
  cloud) actually do work. Of those, the highest-value rows are the
  ones the LLMs DISAGREE on, because they exercise the cascade's
  escalation logic. Of those, we only want rows that aren't already
  represented in the current overlay (no duplicates).

This script applies that filter:

  Pass 1 — rules-miss filter:
    Run the keyword baseline on every row. Drop rows where rules
    would resolve at >= 0.85 confidence (the production threshold).

  Pass 2 — overlay dedupe:
    Drop rows whose `message_id` already exists in the current
    operator overlay (eval/local_email_classification_dataset_overlay.jsonl).

  Output:
    A small CSV at /tmp/curated_legacy_eval_candidates.csv with one
    row per kept example, plus a `keep_in_eval` column the operator
    fills in (yes/no/edit) before we merge.

Usage:

  python -m scripts.curate_legacy_eval_dataset

  python -m scripts.curate_legacy_eval_dataset \\
      --output /tmp/eval-candidates.csv \\
      --diverse-cap 80    # cap kept rows to 80 with diverse-bucket sampling

After review, merge into the overlay:

  python -m scripts.merge_curated_eval_into_overlay \\
      --reviewed /tmp/eval-candidates-reviewed.csv

  (The merge script exists at the path above; if not yet built, fall
  back to the manual procedure documented in the bottom of this file.)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.shared.config import get_settings
from app.tasks.email_classification import _build_rules_tier
from app.workers.email_classification_dataset import (
    load_labeled_email_classification_dataset_with_overlay,
)
from app.workers.email_models import LabeledEmailDatasetExample

DEFAULT_DATASET_PATH = Path("tests/fixtures/email_classification_dataset.csv")
DEFAULT_OUTPUT_PATH = Path("/tmp/curated_legacy_eval_candidates.csv")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()

    # Load the full labeled dataset (CSV + existing overlay).
    dataset_path = settings.root_dir / DEFAULT_DATASET_PATH
    examples = load_labeled_email_classification_dataset_with_overlay(
        dataset_path,
        overlay_path=settings.local_email_classification_dataset_overlay_path,
    )
    overlay_message_ids = _load_overlay_message_ids(
        settings.local_email_classification_dataset_overlay_path
    )

    print(f"loaded {len(examples)} labeled examples from "
          f"{dataset_path.name} + overlay")
    print(f"overlay currently contains {len(overlay_message_ids)} "
          "message_ids")
    print()

    # Pass 1: run rules tier on each. Drop ANY row where rules fires at
    # production confidence (>= 0.85), regardless of whether the rule
    # got it right — production never reaches the LLM in those cases,
    # so they don't belong in LLM regression. They belong in a separate
    # rule-review queue (eval/rule_review_candidates.jsonl) when the
    # rule's prediction conflicts with the human label.
    rules_tier = _build_rules_tier(settings, threshold=0.85)
    rules_resolves = 0
    rule_conflicts: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]] = []
    rules_misses: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]] = []

    for example in examples:
        input_data = _example_to_input(example)
        try:
            prediction = rules_tier.predict(input_data)
        except Exception as exc:
            # Validation error or similar — count as a rules miss
            # (LLM would have to handle it).
            rules_misses.append((
                example,
                {
                    "rules_outcome": f"error: {type(exc).__name__}",
                    "rules_confidence": 0.0,
                },
            ))
            continue
        rule_l1 = (prediction.output or {}).get("level1_classification")
        if not prediction.abstained and prediction.confidence >= 0.85:
            # Rule fires at production confidence → classifier territory.
            if rule_l1 == example.expected_level1_classification:
                rules_resolves += 1  # Classifier handles this correctly; drop.
            else:
                # Classifier handles this CONFIDENTLY but disagrees with
                # the human label. Capture as rule_review candidate.
                rule_conflicts.append((
                    example,
                    {
                        "rules_outcome": "rule_conflict",
                        "rules_confidence": round(prediction.confidence, 3),
                        "rules_predicted": rule_l1,
                    },
                ))
            continue
        # Rules either abstained or fired with low confidence —
        # production falls through to LLM, so this row exercises LLM tiers.
        rules_misses.append((
            example,
            {
                "rules_outcome": (
                    "abstain" if prediction.abstained
                    else "low_confidence"
                ),
                "rules_confidence": round(prediction.confidence, 3),
                "rules_predicted": rule_l1 if not prediction.abstained else None,
            },
        ))

    print(f"rules-resolved (drop, classifier handles correctly): {rules_resolves}")
    print(f"rule-conflicts (act on inline; no backlog file): {len(rule_conflicts)}")
    print(f"LLM candidates (rules abstained or low-conf): {len(rules_misses)}")

    # No rule-review queue. Print conflicts inline so the operator
    # decides on the spot — edit the rule (Loop 4) OR discard the row
    # because the rule is right and the label was wrong. Persistent
    # backlog files for these accumulate stale decisions; not allowed.
    if rule_conflicts:
        print()
        print("─── rule conflicts (decide now: edit rule via Loop 4, or discard row) ───")
        for example, meta in rule_conflicts:
            print(f"  rule says {str(meta.get('rules_predicted')):12s} "
                  f"({meta.get('rules_confidence')})  | "
                  f"operator says {example.expected_level1_classification:12s}  | "
                  f"from {example.from_email}")
            print(f"      subject: {example.subject[:80]}")

    # Pass 2: drop rows whose message_id is already in the overlay.
    deduped: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]] = []
    duplicates = 0
    for example, meta in rules_misses:
        if example.message_id in overlay_message_ids:
            duplicates += 1
            continue
        deduped.append((example, meta))

    print(f"already in overlay (drop): {duplicates}")
    print(f"new candidates: {len(deduped)}")

    # Pass 3 (optional): cap to N rows with bucket-balanced sampling
    # so the operator gets a tractable review set.
    if args.diverse_cap > 0 and len(deduped) > args.diverse_cap:
        deduped = _diverse_sample(deduped, cap=args.diverse_cap)
        print(f"capped to {len(deduped)} rows via diverse-bucket sampling")
    print()

    # Distribution of kept rows by bucket — sanity check.
    bucket_counts: Counter = Counter(
        ex.expected_level1_classification for ex, _ in deduped
    )
    print("kept rows by bucket:")
    for bucket, count in bucket_counts.most_common():
        print(f"  {bucket:18s} {count:4d}")
    print()

    # Write the CSV for review.
    output_path = Path(args.output)
    _write_review_csv(rows=deduped, path=output_path)
    print(f"wrote {len(deduped)} rows to {output_path}")
    print()
    print("Next:")
    print(f"  1. Open {output_path} in your spreadsheet tool")
    print("  2. Set `keep_in_eval` to 'yes' for rows you want in the")
    print("     eval set, 'no' to drop, or leave blank to keep")
    print("  3. Optionally fix `expected_level1_classification` if a")
    print("     row's label is stale per the new taxonomy")
    print("  4. Save the reviewed CSV (suffix `-reviewed.csv` keeps it")
    print("     distinct from the proposal file)")
    print("  5. Merge into the overlay (manual procedure below until")
    print("     `merge_curated_eval_into_overlay` ships):")
    print()
    print("     For each row marked keep=yes, append a JSON line to")
    print("     $SAI_PRIVATE/eval/local_email_classification_dataset_overlay.jsonl")
    print("     in the EmailClassificationDatasetOverlayRow shape.")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="curate_legacy_eval_dataset", description=__doc__
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH),
                        help=f"CSV output path (default {DEFAULT_OUTPUT_PATH})")
    parser.add_argument("--diverse-cap", type=int, default=80,
                        help="cap kept rows to this many via diverse-bucket "
                             "sampling (0 = no cap, keep all). Default 80.")
    return parser.parse_args(argv)


def _load_overlay_message_ids(overlay_path: Path) -> set[str]:
    """Read the overlay JSONL and return the set of message_ids
    already represented. Empty set if the file doesn't exist yet.
    """

    if not overlay_path.exists():
        return set()
    ids: set[str] = set()
    with overlay_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = row.get("message_id")
            if mid:
                ids.add(str(mid))
    return ids


def _example_to_input(example: LabeledEmailDatasetExample) -> dict[str, Any]:
    """Strip tracking fields and return the EmailMessage-shaped dict
    that the rules tier expects.
    """

    return {
        "message_id": example.message_id,
        "thread_id": example.thread_id,
        "from_email": example.from_email,
        "from_name": example.from_name,
        "to": list(example.to),
        "cc": list(example.cc),
        "subject": example.subject,
        "snippet": example.snippet,
        "body_excerpt": example.body_excerpt or "",
        "list_unsubscribe": [],
        "list_unsubscribe_post": None,
        "unsubscribe_links": [],
        "received_at": (
            example.received_at.isoformat() if example.received_at else None
        ),
    }


def _diverse_sample(
    rows: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]],
    *,
    cap: int,
) -> list[tuple[LabeledEmailDatasetExample, dict[str, Any]]]:
    """Round-robin sample across human labels so all buckets get
    representation, even ones with few examples.

    Keeps the operator's review tractable (~80 rows by default)
    without dropping minority buckets. The first row from each bucket
    survives even if the cap forces aggressive trimming on bigger
    buckets.
    """

    by_bucket: dict[str, list[tuple[LabeledEmailDatasetExample, dict[str, Any]]]] = (
        defaultdict(list)
    )
    for ex, meta in rows:
        by_bucket[ex.expected_level1_classification].append((ex, meta))

    selected: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]] = []
    while len(selected) < cap and any(by_bucket.values()):
        for bucket in list(by_bucket.keys()):
            if len(selected) >= cap:
                break
            if not by_bucket[bucket]:
                continue
            selected.append(by_bucket[bucket].pop(0))
    return selected


def _write_review_csv(
    *,
    rows: list[tuple[LabeledEmailDatasetExample, dict[str, Any]]],
    path: Path,
) -> None:
    """One row per candidate. The `keep_in_eval` column is the
    operator's review verdict — yes / no / blank-keeps.
    """

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "keep_in_eval",
            "expected_level1_classification",
            "from_email",
            "from_name",
            "subject",
            "snippet",
            "body_excerpt",
            "rules_outcome",
            "rules_predicted",
            "rules_confidence",
            "message_id",
            "thread_id",
            "operator_notes",
        ])
        for example, meta in rows:
            writer.writerow([
                "",  # keep_in_eval — operator fills in
                example.expected_level1_classification,
                example.from_email,
                example.from_name or "",
                example.subject,
                (example.snippet or "")[:500],
                (example.body_excerpt or "")[:2000],
                meta.get("rules_outcome", ""),
                meta.get("rules_predicted", "") or "",
                meta.get("rules_confidence", ""),
                example.message_id,
                example.thread_id or "",
                "",  # operator_notes — free-form
            ])


if __name__ == "__main__":
    sys.exit(main())
