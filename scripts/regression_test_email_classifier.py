"""Regression-test the new email_classification cascade against ground truth.

Replays every example in the labeled dataset (CSV + JSONL overlay) through
the new `TieredTaskRunner`-based email classification task. For each
example, compares the cascade's `level1_classification` decision against
the human-certified expected label.

Reports:
  - Overall L1 accuracy
  - Per-bucket precision / recall / F1
  - Per-tier resolution share (which tier resolved each input)
  - Per-tier accuracy (when a tier resolved, was it right?)
  - Confusion matrix
  - Cost breakdown (mean cost per request, total)

Output: stdout report + optional --report-out JSON for tracking.

Usage:
    python -m scripts.regression_test_email_classifier --limit 50
    python -m scripts.regression_test_email_classifier --report-out /tmp/regression.json
    python -m scripts.regression_test_email_classifier --bucket customers  # filter
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.eval.datasets import EdgeCaseRow
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack import TieredTaskRunner
from app.shared.config import get_settings
from app.tasks.email_classification import build_email_classification_task
from app.workers.email_classification_dataset import (
    load_labeled_email_classification_dataset_with_overlay,
)
from app.workers.email_models import LabeledEmailDatasetExample, LEVEL1_DISPLAY_NAMES

DEFAULT_DATASET_PATH = Path("tests/fixtures/email_classification_dataset.csv")
DEFAULT_EDGE_CASES_PATH = Path("eval/edge_cases.jsonl")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Load runtime.env so any op:// / keychain:// secret references
    # resolve before Settings reads them.
    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()

    # Load Loop-1 LLM eval set from the canonical edge_cases.jsonl
    # (per PRINCIPLES.md §16a). The legacy fixture+overlay loader is
    # only used when --legacy-fixture is passed (kept for one-off
    # historical comparisons; never the default).
    if args.legacy_fixture:
        dataset_path = settings.root_dir / DEFAULT_DATASET_PATH
        examples = load_labeled_email_classification_dataset_with_overlay(
            dataset_path,
            overlay_path=settings.local_email_classification_dataset_overlay_path,
        )
        source_label = f"{dataset_path.name} + legacy overlay"
    else:
        edge_cases_path = settings.root_dir / DEFAULT_EDGE_CASES_PATH
        if not edge_cases_path.exists():
            print(f"error: edge cases file missing: {edge_cases_path}",
                  file=sys.stderr)
            print("  generate with `sai eval add-edge-case` (Phase 4) or "
                  "convert from your overlay first.", file=sys.stderr)
            return 2
        examples = _load_edge_cases_as_examples(edge_cases_path)
        source_label = f"{edge_cases_path.relative_to(settings.root_dir)}"

    if args.bucket:
        examples = [e for e in examples if e.expected_level1_classification == args.bucket]
    if args.limit > 0:
        examples = examples[: args.limit]
    if not examples:
        print("error: no examples to evaluate (filter too narrow?)", file=sys.stderr)
        return 2
    print(f"loaded {len(examples)} examples from {source_label}")

    # Local-only mode (used by the apply gate per PRINCIPLES.md §16b):
    # disables the cloud tier so the regression is free. Cascade falls
    # through to USE_ACTIVE on local's output.
    import os as _os
    disable_cloud = _os.environ.get("SAI_REGRESSION_DISABLE_CLOUD", "").lower() in {"1", "true", "yes"}

    task = build_email_classification_task(
        settings=settings,
        cloud_model=args.cloud_model,
        local_model=args.local_model,
        # Regression has ground truth already — DO NOT pollute production
        # Slack with asks. disable_human_tier=True forces USE_ACTIVE
        # escalation: cloud's output (or empty if it abstained) gets
        # applied without posting anything to Slack.
        disable_human_tier=True,
        disable_cloud_tier=disable_cloud,
    )
    if disable_cloud:
        print("  cloud tier DISABLED (SAI_REGRESSION_DISABLE_CLOUD=1)")
    # Use a tmp eval store so we don't pollute production records.
    import tempfile
    tmp_eval_dir = Path(tempfile.mkdtemp(prefix="sai_regression_"))
    runner = TieredTaskRunner(eval_store=EvalRecordStore(root=tmp_eval_dir))
    print(f"using TaskFactory: active_tier_id={task.config.active_tier_id} "
          f"escalation_policy={task.config.escalation_policy.value}")
    print(f"  tiers: {[(t.tier_id, t.tier_kind.value) for t in task.tiers]}")
    print(f"  ephemeral eval store: {tmp_eval_dir}")
    print()

    rows = _run_evaluation(runner=runner, task=task, examples=examples)
    metrics = _compute_metrics(rows)
    _print_report(metrics, args=args)

    if args.report_out:
        Path(args.report_out).write_text(
            json.dumps(metrics, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwrote machine report → {args.report_out}")
    return 0 if metrics["overall_accuracy"] >= args.min_accuracy else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="regression_test_email_classifier",
        description=__doc__,
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="max examples to evaluate (0 = all)")
    parser.add_argument("--bucket", default=None,
                        help="only evaluate examples with this expected L1 bucket")
    parser.add_argument("--cloud-model", default="gpt-5.2-pro",
                        help="OpenAI model for cloud_llm tier")
    parser.add_argument("--local-model", default=None,
                        help="Ollama model for local_llm tier (override "
                             "settings.local_llm_model). qwen2.5:7b is "
                             "reliable for structured output; gpt-oss:20b "
                             "returns empty bodies (harmony reasoning).")
    parser.add_argument("--report-out", type=Path, default=None,
                        help="write JSON report to this path")
    parser.add_argument("--min-accuracy", type=float, default=0.0,
                        help="exit 1 if overall L1 accuracy < this")
    parser.add_argument("--legacy-fixture", action="store_true",
                        help="load from tests/fixtures/email_classification_dataset.csv "
                             "+ overlay (pre-2026-05-01 noisy mix); default is "
                             "eval/edge_cases.jsonl per PRINCIPLES.md §16a")
    return parser.parse_args(argv)


def _load_edge_cases_as_examples(path: Path) -> list[LabeledEmailDatasetExample]:
    """Read EdgeCaseRows from the canonical edge_cases.jsonl and return
    them as LabeledEmailDatasetExamples for the existing eval pipeline.
    """

    rows = [EdgeCaseRow.model_validate_json(line)
            for line in path.read_text().splitlines() if line.strip()]
    examples: list[LabeledEmailDatasetExample] = []
    for r in rows:
        examples.append(LabeledEmailDatasetExample(
            message_id=r.message_id,
            thread_id=r.thread_id,
            from_email=r.from_email,
            from_name=r.from_name,
            to=r.to,
            cc=r.cc,
            subject=r.subject,
            snippet=r.snippet,
            body_excerpt=r.body_excerpt or r.snippet,
            body=r.body,
            received_at=r.received_at,
            source_label=r.edge_case_id,
            expected_level1_classification=r.expected_level1_classification,
            expected_level2_intent=r.expected_level2_intent,
            raw_level1_label=r.raw_level1_label,
            raw_level2_label=r.raw_level2_label,
        ))
    return examples


def _run_evaluation(
    *,
    runner: TieredTaskRunner,
    task,
    examples: list[LabeledEmailDatasetExample],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, example in enumerate(examples, start=1):
        if index % 10 == 0 or index == len(examples):
            print(f"  [{index}/{len(examples)}] processing...", end="\r")
        input_data = _example_to_input(example)
        try:
            record = runner.run(
                task,
                input_id=example.message_id,
                input_data=input_data,
            )
        except Exception as exc:
            rows.append({
                "message_id": example.message_id,
                "expected_l1": example.expected_level1_classification,
                "predicted_l1": None,
                "error": f"{type(exc).__name__}: {exc}",
                "escalation_chain": [],
                "resolved_by": None,
                "cost_usd": 0.0,
                "abstain_reasons": {},
            })
            continue

        decision = record.active_decision or {}
        predicted_l1 = decision.get("level1_classification") or decision.get("label")
        resolved_by = record.escalation_chain[-1] if record.escalation_chain else None
        total_cost = sum(
            float(p.cost_usd) for p in record.tier_predictions.values()
        )
        # Capture abstain reasoning per tier — diagnostic for "why is
        # cloud_llm silent?" / "why did all 35 cases fall through to human?"
        # Keep up to 2000 chars so vendor 400/404 error bodies land in full.
        abstain_reasons: dict[str, str] = {}
        for tier_id, prediction in record.tier_predictions.items():
            if prediction.abstained and prediction.reasoning:
                abstain_reasons[tier_id] = prediction.reasoning[:2000]
        rows.append({
            "message_id": example.message_id,
            "expected_l1": example.expected_level1_classification,
            "predicted_l1": predicted_l1,
            "escalation_chain": list(record.escalation_chain),
            "resolved_by": resolved_by,
            "cost_usd": round(total_cost, 6),
            "abstain_reasons": abstain_reasons,
        })
    print()
    return rows


def _example_to_input(example: LabeledEmailDatasetExample) -> dict[str, Any]:
    """Convert a LabeledEmailDatasetExample to the dict shape the runner expects."""

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
        "received_at": example.received_at.isoformat() if example.received_at else None,
    }


def _compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    errors = [r for r in rows if r.get("error")]
    completed = [r for r in rows if not r.get("error")]
    correct = [r for r in completed if r["predicted_l1"] == r["expected_l1"]]

    overall_accuracy = (len(correct) / total) if total else 0.0
    completed_accuracy = (len(correct) / len(completed)) if completed else 0.0

    # Per-bucket P/R/F1
    buckets = sorted({r["expected_l1"] for r in rows} | {r["predicted_l1"] for r in rows if r.get("predicted_l1")})
    per_bucket: dict[str, dict[str, float]] = {}
    for bucket in buckets:
        if not bucket:
            continue
        tp = sum(1 for r in completed if r["expected_l1"] == bucket and r["predicted_l1"] == bucket)
        fp = sum(1 for r in completed if r["expected_l1"] != bucket and r["predicted_l1"] == bucket)
        fn = sum(1 for r in completed if r["expected_l1"] == bucket and r["predicted_l1"] != bucket)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_bucket[bucket] = {
            "support": tp + fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
        }

    # Per-tier resolution + accuracy
    tier_resolutions: Counter = Counter()
    tier_correct: Counter = Counter()
    for r in completed:
        resolved_by = r.get("resolved_by") or "(no_resolver)"
        tier_resolutions[resolved_by] += 1
        if r["predicted_l1"] == r["expected_l1"]:
            tier_correct[resolved_by] += 1
    per_tier = {
        tier: {
            "resolved": resolved,
            "correct": tier_correct[tier],
            "accuracy_when_resolved": round(tier_correct[tier] / resolved, 3) if resolved else 0.0,
        }
        for tier, resolved in tier_resolutions.most_common()
    }

    # Confusion matrix (top mismatches)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in completed:
        if r["predicted_l1"] != r["expected_l1"]:
            confusion[r["expected_l1"]][r["predicted_l1"] or "(none)"] += 1
    confusion_top: list[dict[str, Any]] = []
    for expected, predictions in confusion.items():
        for predicted, count in predictions.items():
            confusion_top.append({"expected": expected, "predicted": predicted, "count": count})
    confusion_top.sort(key=lambda item: item["count"], reverse=True)

    total_cost = sum(r.get("cost_usd", 0.0) for r in completed)

    # Top abstain reasons per tier — answers "why is local_llm / cloud_llm
    # silent?" by surfacing the underlying error class (provider error,
    # timeout, low confidence, etc).
    abstain_reason_buckets: dict[str, Counter] = defaultdict(Counter)
    for r in completed:
        for tier_id, reason in (r.get("abstain_reasons") or {}).items():
            # Keep up to 400 chars so vendor 400/404/JSON error bodies
            # land in full ("Unsupported parameter X for model Y" etc.).
            # The diagnostic value is in the FULL message; truncating
            # at 120 collapsed the discriminating part.
            short = reason.strip()[:400] if reason else "(no reason)"
            abstain_reason_buckets[tier_id][short] += 1
    abstain_summary: dict[str, list[dict[str, Any]]] = {
        tier_id: [
            {"reason": reason, "count": count}
            for reason, count in counter.most_common(5)
        ]
        for tier_id, counter in abstain_reason_buckets.items()
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_examples": total,
        "completed": len(completed),
        "errors": len(errors),
        "correct": len(correct),
        "overall_accuracy": round(overall_accuracy, 4),
        "completed_accuracy": round(completed_accuracy, 4),
        "per_bucket": per_bucket,
        "per_tier": per_tier,
        "confusion_top": confusion_top[:20],
        "total_cost_usd": round(total_cost, 6),
        "mean_cost_per_request_usd": round(total_cost / total, 6) if total else 0.0,
        "abstain_reasons_by_tier": abstain_summary,
        "rows": rows if len(rows) <= 1000 else None,  # only embed for small runs
    }


def _print_report(metrics: dict[str, Any], *, args: argparse.Namespace) -> None:
    print("=" * 72)
    print("EMAIL CLASSIFICATION REGRESSION REPORT")
    print("=" * 72)
    print()
    print(f"  Examples:           {metrics['total_examples']}")
    print(f"  Completed:          {metrics['completed']}")
    print(f"  Errors:             {metrics['errors']}")
    print(f"  Correct (L1):       {metrics['correct']}")
    print(f"  Overall accuracy:   {metrics['overall_accuracy']:.1%}")
    print(f"  Total cost:         ${metrics['total_cost_usd']:.4f}")
    print(f"  Cost / request:     ${metrics['mean_cost_per_request_usd']:.6f}")
    print()
    print("PER-TIER RESOLUTION:")
    for tier, stats in metrics["per_tier"].items():
        print(f"  {tier:14s}  resolved={stats['resolved']:4d}  "
              f"correct={stats['correct']:4d}  "
              f"acc={stats['accuracy_when_resolved']:.1%}")
    print()
    print(f"PER-BUCKET (display: {LEVEL1_DISPLAY_NAMES.get('customers', 'Customers')} etc):")
    print(f"  {'bucket':15s}  {'support':>7s}  {'P':>5s}  {'R':>5s}  {'F1':>5s}")
    for bucket, stats in metrics["per_bucket"].items():
        print(f"  {bucket:15s}  {stats['support']:>7d}  "
              f"{stats['precision']:>5.2f}  {stats['recall']:>5.2f}  {stats['f1']:>5.2f}")
    print()
    if metrics.get("abstain_reasons_by_tier"):
        print("ABSTAIN REASONS BY TIER (why each tier didn't resolve):")
        for tier_id, reasons in metrics["abstain_reasons_by_tier"].items():
            if not reasons:
                continue
            print(f"  {tier_id}:")
            for entry in reasons:
                print(f"    {entry['count']:3d}×  {entry['reason']}")
        print()

    if metrics["confusion_top"]:
        print("TOP CONFUSIONS (expected → predicted, count):")
        for item in metrics["confusion_top"][:10]:
            print(f"  {item['expected']:15s} → {item['predicted']:15s}  ({item['count']})")
    print()


if __name__ == "__main__":
    sys.exit(main())
