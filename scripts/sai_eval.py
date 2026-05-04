"""Single CLI entry point for the eval system. PRINCIPLES.md §16a.

Usage:
    sai eval generate-canaries        # walk rules config → eval/canaries.jsonl
    sai eval run [--canaries-only]    # two-tier regression (Loop 1)
    sai eval run [--llm-only]         # skip canaries
    sai eval audit                    # show counts of A / B / C / rule-review

Future subcommands (Sessions 2 / 3):
    sai eval add-edge-case --from-csv <path>   # Loop 4 LLM hint
    sai eval add-rule <sender|domain> --l1 X   # Loop 4 classifier change
    sai eval batch-disagreements               # Loop 2 trigger check
    sai eval resolve-batch --reviewed <path>   # Loop 3 ingest

All subcommands respect principle #17 (mechanism public, values
private). The CLI itself takes no operator-specific config; everything
flows through ``Settings``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.shared.config import get_settings


def _cmd_generate_canaries(args: argparse.Namespace) -> int:
    """Delegate to scripts/generate_classifier_canaries.py."""
    from scripts.generate_classifier_canaries import main as gen_main
    forwarded: list[str] = []
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.output:
        forwarded += ["--output", args.output]
    return gen_main(forwarded)


def _cmd_run(args: argparse.Namespace) -> int:
    """Two-tier regression. Canaries first (fail-fast), then LLM eval."""
    from scripts.regression_test_canaries import main as canary_main
    from scripts.regression_test_email_classifier import main as llm_main

    if not args.llm_only:
        rc = canary_main([])
        if rc != 0:
            print("\n✗ canaries failed — stopping. Fix the rules tier first.",
                  file=sys.stderr)
            return rc
        if args.canaries_only:
            return 0

    print()
    print("─" * 60)
    print("LLM edge-case regression")
    print("─" * 60)
    return llm_main([])


def _cmd_batch_disagreements(args: argparse.Namespace) -> int:
    """Loop 2 v1 — surface a batch ask to the operator if queue >= threshold."""
    from scripts.batch_disagreements import main as batch_main
    forwarded: list[str] = []
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.threshold is not None:
        forwarded += ["--threshold", str(args.threshold)]
    return batch_main(forwarded)


def _cmd_audit(args: argparse.Namespace) -> int:
    """Show counts of A / B / C / rule-review for quick visibility."""
    settings = get_settings()
    eval_dir = settings.root_dir / "eval"

    def count(name: str) -> str:
        p = eval_dir / name
        if not p.exists():
            return "(missing)"
        n = sum(1 for line in p.open() if line.strip())
        return f"{n} rows"

    print(f"eval datasets at {eval_dir}:")
    print(f"  A. canaries.jsonl              {count('canaries.jsonl')}")
    print(f"  B. edge_cases.jsonl            {count('edge_cases.jsonl')}  (cap = 50)")
    print(f"  C. disagreement_queue.jsonl    {count('disagreement_queue.jsonl')}  (batch trigger ≥ 50)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sai-eval", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate-canaries", help="walk rules → canaries.jsonl")
    p_gen.add_argument("--output", default=None)
    p_gen.add_argument("--dry-run", action="store_true")
    p_gen.set_defaults(func=_cmd_generate_canaries)

    p_run = sub.add_parser("run", help="two-tier regression (canaries + LLM)")
    p_run.add_argument("--canaries-only", action="store_true")
    p_run.add_argument("--llm-only", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    p_audit = sub.add_parser("audit", help="show dataset counts")
    p_audit.set_defaults(func=_cmd_audit)

    p_batch = sub.add_parser(
        "batch-disagreements",
        help="Loop 2 v1: surface batch ask if queue ≥ threshold",
    )
    p_batch.add_argument("--force", action="store_true")
    p_batch.add_argument("--dry-run", action="store_true")
    p_batch.add_argument("--threshold", type=int, default=None)
    p_batch.set_defaults(func=_cmd_batch_disagreements)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
