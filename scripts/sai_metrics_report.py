"""sai-metrics — text-mode SAI eval / regression / quality report.

Aggregates eval dataset state (per #16a EvalDataset abstraction) +
regression history into a single text-mode report. Cron-firable;
emits text suitable for Slack post (the future #sai-metrics agent
will re-use this as its data source).

Sources:
  - eval/canaries.jsonl                          (rules-tier canaries)
  - eval/edge_cases.jsonl                        (LLM tier; soft-cap 50)
  - eval/disagreements.jsonl                     (Loop 2 queue)
  - eval/<workflow_id>_true_north.jsonl          (per-workflow #16h)
  - eval/proposed/                               (open Loop 4 proposals)
  - ~/Library/Logs/SAI/scheduled/                (last cron runs)

Usage:
  python -m scripts.sai_metrics_report
  python -m scripts.sai_metrics_report --json
  python -m scripts.sai_metrics_report --include-true-north

Per PRINCIPLES.md §16i this CLI is the data source the
``#sai-metrics`` Slack agent (Stage C) will pull from.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path.home() / ".sai-runtime"
EVAL_DIR = RUNTIME_DIR / "eval"
PROPOSED_DIR = EVAL_DIR / "proposed"


@dataclass
class DatasetSummary:
    """One row in the metrics report."""

    name: str
    path: str
    count: int
    soft_cap: int | None
    last_modified: str | None
    notes: str = ""


@dataclass
class ProposalsSummary:
    open_count: int
    oldest_age_hours: float | None
    paths: list[str] = field(default_factory=list)


@dataclass
class MetricsReport:
    captured_at: str
    datasets: list[DatasetSummary] = field(default_factory=list)
    proposals: ProposalsSummary | None = None
    note: str = ""


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    )


def _last_modified_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")


def _scan_datasets(include_true_north: bool) -> list[DatasetSummary]:
    """One row per known eval dataset that exists on disk."""

    datasets: list[DatasetSummary] = []

    canaries_path = EVAL_DIR / "canaries.jsonl"
    if canaries_path.exists():
        datasets.append(DatasetSummary(
            name="canaries",
            path=str(canaries_path),
            count=_count_jsonl(canaries_path),
            soft_cap=None,
            last_modified=_last_modified_iso(canaries_path),
            notes="rules-tier; regenerated from rules config on apply",
        ))

    edge_path = EVAL_DIR / "edge_cases.jsonl"
    if edge_path.exists():
        n = _count_jsonl(edge_path)
        # Read soft_cap from runtime tunables for accurate health note.
        try:
            from app.shared.runtime_tunables import get as _t
            cap = int(_t("edge_case_soft_cap"))
        except Exception:
            cap = 50
        notes_bits = ["LLM tier; cap-gated regression"]
        if n >= cap:
            notes_bits.append(f"AT CAP ({n}/{cap}) — eviction will fire on next add")
        elif n >= 0.8 * cap:
            notes_bits.append(f"NEAR CAP ({n}/{cap})")
        datasets.append(DatasetSummary(
            name="edge_cases",
            path=str(edge_path),
            count=n,
            soft_cap=cap,
            last_modified=_last_modified_iso(edge_path),
            notes="; ".join(notes_bits),
        ))

    disag_path = EVAL_DIR / "disagreements.jsonl"
    if disag_path.exists():
        datasets.append(DatasetSummary(
            name="disagreements",
            path=str(disag_path),
            count=_count_jsonl(disag_path),
            soft_cap=None,
            last_modified=_last_modified_iso(disag_path),
            notes="Loop 2 queue; ≥50 triggers operator batch ask",
        ))

    if include_true_north and EVAL_DIR.exists():
        for tn_path in sorted(EVAL_DIR.glob("*_true_north.jsonl")):
            datasets.append(DatasetSummary(
                name=f"true_north:{tn_path.stem.replace('_true_north', '')}",
                path=str(tn_path),
                count=_count_jsonl(tn_path),
                soft_cap=None,
                last_modified=_last_modified_iso(tn_path),
                notes="uncapped historical; weekly Sunday 3am cron run",
            ))

    return datasets


def _scan_proposals() -> ProposalsSummary:
    if not PROPOSED_DIR.exists():
        return ProposalsSummary(open_count=0, oldest_age_hours=None)
    paths = list(PROPOSED_DIR.glob("proposed_*.yaml"))
    if not paths:
        return ProposalsSummary(open_count=0, oldest_age_hours=None)
    now = datetime.now(UTC)
    oldest_age = max(
        (now - datetime.fromtimestamp(p.stat().st_mtime, UTC)).total_seconds() / 3600
        for p in paths
    )
    return ProposalsSummary(
        open_count=len(paths),
        oldest_age_hours=round(oldest_age, 1),
        paths=[p.name for p in paths],
    )


def collect(*, include_true_north: bool = False) -> MetricsReport:
    datasets = _scan_datasets(include_true_north)
    proposals = _scan_proposals()
    note = ""
    if not datasets:
        note = (
            f"No eval datasets at {EVAL_DIR}. Either no workflow has "
            "shipped yet, or the runtime directory isn't merged. Run "
            "`sai-overlay merge`."
        )
    return MetricsReport(
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        datasets=datasets,
        proposals=proposals,
        note=note,
    )


def format_text(report: MetricsReport) -> str:
    lines: list[str] = []
    lines.append(f"📈 SAI metrics — captured {report.captured_at}")
    lines.append("")
    if not report.datasets:
        lines.append(report.note or "(no data)")
        return "\n".join(lines)

    lines.append("Eval datasets")
    lines.append("─" * 72)
    for d in report.datasets:
        cap_part = f"/ {d.soft_cap}" if d.soft_cap else ""
        lm = d.last_modified or "(never)"
        lines.append(f"  {d.name:<28s}  {d.count:>4d}{cap_part:<7s}  modified {lm}")
        if d.notes:
            lines.append(f"      {d.notes}")
    lines.append("")

    if report.proposals:
        p = report.proposals
        lines.append("Open Loop 4 proposals")
        lines.append("─" * 72)
        if p.open_count == 0:
            lines.append("  (none open)")
        else:
            age = f"{p.oldest_age_hours}h" if p.oldest_age_hours else "?"
            lines.append(f"  {p.open_count} open; oldest {age}")
            for name in p.paths[:5]:
                lines.append(f"    • {name}")
            if len(p.paths) > 5:
                lines.append(f"    … and {len(p.paths) - 5} more")

    if report.note:
        lines.append("")
        lines.append(report.note)
    return "\n".join(lines)


def format_json(report: MetricsReport) -> str:
    payload = asdict(report)
    return json.dumps(payload, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sai-metrics-report")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--include-true-north", action="store_true",
        help="include per-workflow true-north datasets in the report",
    )
    args = parser.parse_args(argv)

    report = collect(include_true_north=args.include_true_north)
    if args.json:
        print(format_json(report))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
