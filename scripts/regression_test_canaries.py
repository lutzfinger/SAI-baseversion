"""Regression-test the classifier canaries.

Per the two-tier regression principle (PRINCIPLES.md, 2026-05-01):
canaries run BEFORE the LLM regression. Any canary miss is a hard
fail — production wouldn't reach the LLM tier for these inputs, so a
canary regression means the rules tier itself is broken.

For each canary in eval/classifier_canaries.jsonl:
  1. Build the synthetic EmailMessage from `synthetic_email`.
  2. Feed it through the rules tier (NOT the full cascade).
  3. Assert: prediction.confidence >= canary.min_confidence
     AND prediction.output.level1_classification == canary.expected_level1_classification.

Reports one of:
  - PASS: every canary fires correctly with sufficient confidence.
  - FAIL: list of every miss, with expected vs. actual + reason.

Exit code 0 = pass, non-zero = fail.

Usage:
    python -m scripts.regression_test_canaries
    python -m scripts.regression_test_canaries --canaries /tmp/canaries.jsonl
    python -m scripts.regression_test_canaries --kind sender_email  # filter
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.shared.config import get_settings
from app.tasks.email_classification import _build_rules_tier


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()
    canary_path = (
        Path(args.canaries).expanduser() if args.canaries
        else settings.root_dir / "eval" / "canaries.jsonl"
    )
    if not canary_path.exists():
        print(f"error: canary file not found: {canary_path}", file=sys.stderr)
        print("  run `python -m scripts.generate_classifier_canaries` first.",
              file=sys.stderr)
        return 2

    canaries = [json.loads(l) for l in canary_path.open() if l.strip()]
    if args.kind:
        canaries = [c for c in canaries if c["rule_kind"] == args.kind]
    if not canaries:
        print(f"error: no canaries to test", file=sys.stderr)
        return 2

    rules_tier = _build_rules_tier(settings, threshold=0.85)
    print(f"loaded {len(canaries)} canaries from {canary_path}")
    print(f"rules tier built; threshold = 0.85")
    print()

    misses: list[dict[str, Any]] = []
    by_kind_passed: Counter = Counter()
    by_kind_failed: Counter = Counter()
    for canary in canaries:
        result = _run_canary(canary, rules_tier=rules_tier)
        if result["pass"]:
            by_kind_passed[canary["rule_kind"]] += 1
        else:
            by_kind_failed[canary["rule_kind"]] += 1
            misses.append({**canary, **result})

    n_pass = sum(by_kind_passed.values())
    n_fail = sum(by_kind_failed.values())

    print(f"results: {n_pass} pass / {n_fail} fail / {len(canaries)} total")
    print()
    print("by rule_kind:")
    all_kinds = sorted(set(by_kind_passed) | set(by_kind_failed))
    for kind in all_kinds:
        p, f = by_kind_passed[kind], by_kind_failed[kind]
        marker = "✓" if f == 0 else "✗"
        print(f"  {marker} {kind:36s} pass={p:3d}  fail={f:3d}")
    # Action breakdown — apply_l1_label vs skip_l1_tagging.
    by_action_pass: Counter = Counter()
    by_action_fail: Counter = Counter()
    for canary in canaries:
        action = canary.get("expected_action", "apply_l1_label")
        if any(m["rule_id"] == canary["rule_id"] for m in misses):
            by_action_fail[action] += 1
        else:
            by_action_pass[action] += 1
    print()
    print("by expected action:")
    for action in sorted(set(by_action_pass) | set(by_action_fail)):
        p, f = by_action_pass[action], by_action_fail[action]
        marker = "✓" if f == 0 else "✗"
        print(f"  {marker} {action:20s} pass={p:3d}  fail={f:3d}")
    print()

    if misses:
        print(f"failures ({len(misses)}):")
        for m in misses[:20]:
            action = m.get("expected_action", "apply_l1_label")
            tag = "[skip]" if action == "skip_l1_tagging" else f"[{m['expected_level1_classification']}]"
            print(f"  ✗ {tag:14s} {m['rule_id']}")
            print(f"      expected: action={action}  L1={m['expected_level1_classification']} "
                  f"conf>={m['min_confidence']}")
            print(f"      actual:   L1={m.get('actual_l1')} "
                  f"conf={m.get('actual_confidence')}")
            print(f"      why:      {m.get('miss_reason')}")
        if len(misses) > 20:
            print(f"  ... and {len(misses) - 20} more (use --report-out for full list)")

    if args.report_out:
        report = {
            "total": len(canaries),
            "passed": n_pass,
            "failed": n_fail,
            "by_kind": {
                k: {"pass": by_kind_passed[k], "fail": by_kind_failed[k]}
                for k in all_kinds
            },
            "misses": misses,
        }
        Path(args.report_out).write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nfull report → {args.report_out}")

    return 0 if n_fail == 0 else 1


def _run_canary(canary: dict[str, Any], *, rules_tier) -> dict[str, Any]:
    msg = canary["synthetic_email"]
    expected_l1 = canary["expected_level1_classification"]
    min_conf = canary.get("min_confidence", 0.85)

    # Strip the canary-only fields the rules tier doesn't expect.
    rules_input = {k: v for k, v in msg.items()
                   if k not in {"directly_addressed", "is_thread_start"}}

    try:
        prediction = rules_tier.predict(rules_input)
    except Exception as exc:
        return {
            "pass": False,
            "actual_l1": None,
            "actual_confidence": None,
            "miss_reason": f"prediction raised {type(exc).__name__}: {exc}",
        }

    actual_l1 = (prediction.output or {}).get("level1_classification")
    actual_conf = round(prediction.confidence, 3) if prediction.confidence is not None else 0.0

    if prediction.abstained:
        return {
            "pass": False,
            "actual_l1": None,
            "actual_confidence": actual_conf,
            "miss_reason": "rules tier abstained (rule did not fire)",
        }
    if actual_l1 != expected_l1:
        return {
            "pass": False,
            "actual_l1": actual_l1,
            "actual_confidence": actual_conf,
            "miss_reason": f"wrong L1 (expected {expected_l1!r}, got {actual_l1!r})",
        }
    if actual_conf < min_conf:
        return {
            "pass": False,
            "actual_l1": actual_l1,
            "actual_confidence": actual_conf,
            "miss_reason": f"confidence {actual_conf} below required {min_conf}",
        }
    return {
        "pass": True,
        "actual_l1": actual_l1,
        "actual_confidence": actual_conf,
        "miss_reason": None,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="regression_test_canaries", description=__doc__
    )
    p.add_argument("--canaries", default=None,
                   help="canary JSONL path (default: <repo>/eval/classifier_canaries.jsonl)")
    p.add_argument("--kind", default=None,
                   help="only run canaries with this rule_kind")
    p.add_argument("--report-out", default=None,
                   help="write a JSON report with full miss details")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
