"""sai-cost — text-mode SAI internal cost report.

Aggregates SAI's own audit log + sai_eval_agent invocation log into
a per-workflow + per-LLM-role cost breakdown. Cron-firable; emits
text suitable for Slack post (the future #sai-cost agent will
re-use this as its data source — see
docs/design_cost_dashboard_slack.md).

Distinction from ``app/workers/daily_cost_report.py``: that worker
posts PROVIDER-API totals (OpenAI org-cost endpoint, Gemini billing
API). This script posts SAI's own breakdown — what each workflow
+ tier + role spent. The two are complementary.

Usage:
  python -m scripts.sai_cost_report                # today
  python -m scripts.sai_cost_report --hours 24
  python -m scripts.sai_cost_report --json
  python -m scripts.sai_cost_report --hours 168    # last week

Per PRINCIPLES.md §16i this CLI is the data source the
``#sai-cost`` Slack agent (Stage C) will pull from. The agent
adds natural-language query handling on top; the aggregation is
identical.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / "Library" / "Logs" / "SAI"
AUDIT_PATH = LOG_DIR / "audit.jsonl"
SAI_EVAL_AGENT_LOG = LOG_DIR / "sai_eval_agent.jsonl"


@dataclass
class CostRow:
    """One row in the per-workflow cost breakdown."""

    workflow: str
    invocations: int = 0
    cost_usd: float = 0.0

    def avg_cost(self) -> float:
        return self.cost_usd / self.invocations if self.invocations else 0.0


@dataclass
class CostReport:
    captured_at: str
    window_hours: int
    rows: list[CostRow] = field(default_factory=list)
    total_invocations: int = 0
    total_cost_usd: float = 0.0
    note: str = ""


def collect(hours: int) -> CostReport:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    by_workflow: dict[str, CostRow] = defaultdict(
        lambda: CostRow(workflow="(unknown)"),
    )

    # Source 1: sai_eval_agent invocation log (per-message agent runs).
    if SAI_EVAL_AGENT_LOG.exists():
        for line in SAI_EVAL_AGENT_LOG.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("started_at", "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if when < cutoff:
                continue
            cost = float(row.get("cost_usd", 0.0) or 0.0)
            wf = "sai-eval-agent"
            by_workflow[wf].workflow = wf
            by_workflow[wf].invocations += 1
            by_workflow[wf].cost_usd += cost

    # Source 2: audit log entries that carry cost in their payload.
    if AUDIT_PATH.exists():
        for line in AUDIT_PATH.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("timestamp", "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if when < cutoff:
                continue
            payload = row.get("payload", {}) or {}
            cost = float(payload.get("cost_usd", 0.0) or 0.0)
            if cost <= 0:
                continue
            wf = (
                row.get("workflow_id")
                or payload.get("workflow_id")
                or row.get("component", "(unknown)")
            )
            by_workflow[wf].workflow = wf
            by_workflow[wf].invocations += 1
            by_workflow[wf].cost_usd += cost

    rows = sorted(by_workflow.values(), key=lambda r: r.cost_usd, reverse=True)
    total_inv = sum(r.invocations for r in rows)
    total_cost = sum(r.cost_usd for r in rows)
    note = ""
    if not rows:
        note = (
            "No cost data in this window. Either nothing ran, or the "
            "audit / sai_eval_agent log doesn't carry cost_usd in its "
            "payload yet (legacy workers don't always emit it)."
        )
    return CostReport(
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_hours=hours,
        rows=rows,
        total_invocations=total_inv,
        total_cost_usd=total_cost,
        note=note,
    )


def format_text(report: CostReport) -> str:
    lines: list[str] = []
    lines.append(
        f"📊 SAI internal cost report — last {report.window_hours}h "
        f"(captured {report.captured_at})"
    )
    lines.append("")
    if not report.rows:
        lines.append(report.note or "(no data)")
        return "\n".join(lines)

    lines.append("Workflow                       Invocations    Cost (USD)    $/run")
    lines.append("─" * 72)
    for r in report.rows:
        lines.append(
            f"{r.workflow[:30]:<30s}   {r.invocations:>11d}   "
            f"${r.cost_usd:>10.4f}   ${r.avg_cost():>6.4f}"
        )
    lines.append("─" * 72)
    lines.append(
        f"{'TOTAL':<30s}   {report.total_invocations:>11d}   "
        f"${report.total_cost_usd:>10.4f}"
    )
    if report.note:
        lines.append("")
        lines.append(report.note)
    return "\n".join(lines)


def format_json(report: CostReport) -> str:
    payload = asdict(report)
    return json.dumps(payload, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sai-cost-report")
    parser.add_argument("--hours", type=int, default=24, help="window size in hours (default 24)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    report = collect(hours=args.hours)
    if args.json:
        print(format_json(report))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
