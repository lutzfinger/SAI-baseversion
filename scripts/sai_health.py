"""sai-health — text-mode operational health snapshot.

Quick "is everything OK right now" summary for the operator. Reads
existing artifacts (launchctl, audit logs, eval files, proposed
proposals) and prints a one-screen status block.

Standalone CLI; no daemon, no LLM calls, no network.

  $ python -m scripts.sai_health
  $ python -m scripts.sai_health --json    # machine-readable

When the Slack-based metrics dashboard ships
(docs/design_cost_dashboard_slack.md), this CLI becomes the
underlying data source — same aggregation, different output format.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

LOG_DIR = Path.home() / "Library" / "Logs" / "SAI"
SCHEDULED_LOG_DIR = LOG_DIR / "scheduled"
RUNTIME_DIR = Path.home() / ".sai-runtime"


@dataclass
class HealthSnapshot:
    captured_at: str
    services: list[dict[str, Any]] = field(default_factory=list)
    bot: dict[str, Any] = field(default_factory=dict)
    eval_state: dict[str, Any] = field(default_factory=dict)
    proposals: dict[str, Any] = field(default_factory=dict)
    audit_24h: dict[str, Any] = field(default_factory=dict)
    errors_24h: dict[str, Any] = field(default_factory=dict)
    overall: str = "unknown"


def collect() -> HealthSnapshot:
    snap = HealthSnapshot(captured_at=datetime.now(UTC).isoformat(timespec="seconds"))
    snap.services = _scheduled_services()
    snap.bot = _slack_bot_state()
    snap.eval_state = _eval_state()
    snap.proposals = _proposals_state()
    snap.audit_24h = _audit_summary(hours=24)
    snap.errors_24h = _error_summary(hours=24)
    snap.overall = _grade(snap)
    return snap


# ─── individual collectors ────────────────────────────────────────────


def _scheduled_services() -> list[dict[str, Any]]:
    """List launchd-loaded com.sai.* services and last-run timestamps."""

    out: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        return [{"error": f"launchctl unavailable: {exc}"}]
    if proc.returncode != 0:
        return [{"error": f"launchctl returned {proc.returncode}"}]

    for line in proc.stdout.splitlines():
        if "com.sai." not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str, last_exit_str, label = parts[0], parts[1], parts[2]
        last_log = _last_log_mtime(label)
        out.append({
            "label": label.strip(),
            "running_pid": pid_str.strip() if pid_str.strip().isdigit() else None,
            "last_exit": last_exit_str.strip(),
            "last_log_mtime": last_log,
        })
    return out


def _slack_bot_state() -> dict[str, Any]:
    """Look for an active slack_bot process + recent launchd log."""

    state: dict[str, Any] = {
        "process_running": False,
        "pid": None,
        "started_at": None,
        "last_stdout_line": None,
    }
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "scripts.slack_bot"],
            capture_output=True, text=True, timeout=3,
        )
        pids = [p.strip() for p in proc.stdout.split() if p.strip().isdigit()]
        if pids:
            state["process_running"] = True
            state["pid"] = int(pids[-1])
            try:
                ps = subprocess.run(
                    ["ps", "-p", str(pids[-1]), "-o", "lstart="],
                    capture_output=True, text=True, timeout=3,
                )
                state["started_at"] = ps.stdout.strip() or None
            except Exception:
                pass
    except Exception as exc:
        state["error"] = str(exc)

    out_log = SCHEDULED_LOG_DIR / "launchd_slack_bot.out.log"
    if out_log.exists():
        try:
            tail = subprocess.run(
                ["tail", "-1", str(out_log)],
                capture_output=True, text=True, timeout=3,
            )
            state["last_stdout_line"] = tail.stdout.strip()[:200] or None
        except Exception:
            pass

    return state


def _eval_state() -> dict[str, Any]:
    """Row counts on the three eval datasets."""

    eval_dir = RUNTIME_DIR / "eval"
    return {
        "canaries_count": _line_count(eval_dir / "canaries.jsonl"),
        "edge_cases_count": _line_count(eval_dir / "edge_cases.jsonl"),
        "disagreement_queue_count": _line_count(eval_dir / "disagreement_queue.jsonl"),
        "edge_cases_soft_cap": 50,
        "disagreement_batch_threshold": 50,
    }


def _proposals_state() -> dict[str, Any]:
    """Open Loop 4 proposals + how long they've been waiting."""

    proposed_dir = RUNTIME_DIR / "eval" / "proposed"
    if not proposed_dir.exists():
        return {"count": 0, "oldest_age_hours": None}
    yamls = sorted(proposed_dir.glob("*.yaml"))
    if not yamls:
        return {"count": 0, "oldest_age_hours": None}
    oldest_mtime = min(p.stat().st_mtime for p in yamls)
    age_hours = (datetime.now(UTC).timestamp() - oldest_mtime) / 3600
    return {
        "count": len(yamls),
        "oldest_age_hours": round(age_hours, 1),
    }


def _audit_summary(*, hours: int) -> dict[str, Any]:
    """Roll up agent + cascade audit rows in the last N hours."""

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out: dict[str, Any] = {
        "agent_invocations": _count_jsonl_after(
            LOG_DIR / "sai_eval_agent.jsonl", cutoff,
        ),
        "agent_total_cost_usd": _sum_jsonl_field_after(
            LOG_DIR / "sai_eval_agent.jsonl", cutoff, "cost_usd",
        ),
    }
    return out


def _error_summary(*, hours: int) -> dict[str, Any]:
    """Recent error counts from launchd error logs."""

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    error_logs = list(SCHEDULED_LOG_DIR.glob("*.err.log")) if SCHEDULED_LOG_DIR.exists() else []
    total = 0
    by_service: dict[str, int] = {}
    for log_path in error_logs:
        if log_path.stat().st_size == 0:
            continue
        # Only count lines added in the last `hours` window — best-effort
        # via tail + mtime filter.
        if log_path.stat().st_mtime < cutoff.timestamp():
            continue
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                lines = sum(
                    1 for line in fh
                    if line.strip() and any(
                        kw in line.lower()
                        for kw in ("error", "exception", "traceback", "failed")
                    )
                )
        except OSError:
            continue
        if lines:
            label = log_path.stem.replace("launchd_", "").replace(".err", "")
            by_service[label] = lines
            total += lines
    return {"total": total, "by_service": by_service}


# ─── helpers ──────────────────────────────────────────────────────────


def _last_log_mtime(label: str) -> Optional[str]:
    short = label.split(".")[-1].replace("-", "_")
    candidates = [
        SCHEDULED_LOG_DIR / f"launchd_{short}.out.log",
        SCHEDULED_LOG_DIR / f"launchd_{short}.err.log",
    ]
    mtimes = [c.stat().st_mtime for c in candidates if c.exists()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), UTC).isoformat(timespec="seconds")


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text().splitlines() if line.strip())
    except OSError:
        return 0


def _count_jsonl_after(path: Path, cutoff: datetime) -> int:
    if not path.exists():
        return 0
    n = 0
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts_str = row.get("started_at") or row.get("captured_at") or ""
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                continue
            if ts >= cutoff:
                n += 1
    except OSError:
        return 0
    return n


def _sum_jsonl_field_after(
    path: Path, cutoff: datetime, field_name: str,
) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts_str = row.get("started_at") or ""
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                continue
            if ts >= cutoff:
                v = row.get(field_name)
                if isinstance(v, (int, float)):
                    total += float(v)
    except OSError:
        return 0.0
    return round(total, 6)


def _grade(snap: HealthSnapshot) -> str:
    """One-word overall: GREEN / YELLOW / RED based on simple thresholds."""

    if snap.errors_24h.get("total", 0) > 5:
        return "RED"
    if not snap.bot.get("process_running"):
        return "YELLOW"
    if snap.proposals.get("oldest_age_hours") and snap.proposals["oldest_age_hours"] > 24:
        return "YELLOW"
    if snap.eval_state.get("disagreement_queue_count", 0) >= snap.eval_state.get(
        "disagreement_batch_threshold", 50,
    ):
        return "YELLOW"
    if snap.errors_24h.get("total", 0) > 0:
        return "YELLOW"
    return "GREEN"


# ─── output ───────────────────────────────────────────────────────────


def render_text(snap: HealthSnapshot) -> str:
    color_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "unknown": "⚪"}
    lines = [
        f"=== SAI health — {snap.captured_at} ===",
        f"  Overall: {color_emoji.get(snap.overall, '⚪')} {snap.overall}",
        "",
        "Scheduled services (launchctl):",
    ]
    if not snap.services:
        lines.append("  (none loaded)")
    for svc in snap.services:
        if "error" in svc:
            lines.append(f"  ⚠ {svc['error']}")
            continue
        running = "running" if svc["running_pid"] else "idle"
        last = svc.get("last_log_mtime") or "(no log)"
        lines.append(f"  - {svc['label']:35s} {running:7s} last={last}")

    lines.extend([
        "",
        "Slack bot:",
        f"  process_running:  {snap.bot.get('process_running')}",
        f"  pid:              {snap.bot.get('pid')}",
        f"  started_at:       {snap.bot.get('started_at') or '(unknown)'}",
        "",
        "Eval datasets (~/.sai-runtime/eval/):",
        f"  canaries:         {snap.eval_state.get('canaries_count', 0)} rows",
        f"  edge_cases:       {snap.eval_state.get('edge_cases_count', 0)} rows / cap {snap.eval_state.get('edge_cases_soft_cap')}",
        f"  disagreements:    {snap.eval_state.get('disagreement_queue_count', 0)} rows / threshold {snap.eval_state.get('disagreement_batch_threshold')}",
        "",
        "Open Loop 4 proposals:",
        f"  count:            {snap.proposals.get('count', 0)}",
        f"  oldest age:       {snap.proposals.get('oldest_age_hours') or '—'} hours",
        "",
        "Activity (last 24h):",
        f"  agent invocations: {snap.audit_24h.get('agent_invocations', 0)}",
        f"  agent cost:        ${snap.audit_24h.get('agent_total_cost_usd', 0.0):.4f}",
        f"  errors:            {snap.errors_24h.get('total', 0)}",
    ])
    if snap.errors_24h.get("by_service"):
        for svc, n in snap.errors_24h["by_service"].items():
            lines.append(f"    - {svc}: {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sai-health",
        description="Print operational health snapshot.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON (for piping to jq / dashboards).",
    )
    args = parser.parse_args(argv)
    snap = collect()
    if args.json:
        print(json.dumps(asdict(snap), indent=2, sort_keys=True))
    else:
        print(render_text(snap))
    return 0 if snap.overall in ("GREEN", "YELLOW") else 1


if __name__ == "__main__":
    sys.exit(main())
