"""sai-rl-report — query and inspect the RL-from-human-feedback stores.

Reads ScoredTrajectory and RawTrajectory JSONL stores and renders a
queryable text-mode report. No LLM calls, no external services.

Usage:
  python -m scripts.sai_rl_report                     # full summary
  python -m scripts.sai_rl_report --workflow sai-eval # filter by workflow
  python -m scripts.sai_rl_report --source approval_approved
  python -m scripts.sai_rl_report --min-reward 0.0    # only positive
  python -m scripts.sai_rl_report --max-reward 0.0    # only negative
  python -m scripts.sai_rl_report --pairs             # show preference pairs
  python -m scripts.sai_rl_report --tail 10           # last N scored
  python -m scripts.sai_rl_report --json              # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rl.models import RewardSource, ScoredTrajectory
from app.rl.preference_pairs import PreferencePairBuilder
from app.rl.trajectory import RawTrajectory, ScoredTrajectoryStore, TrajectoryStore

# ── default store roots (match Settings defaults) ──────────────────────────
_STATE_DIR  = Path("~/Library/Application Support/SAI/state").expanduser()
_LOGS_DIR   = Path("~/Library/Logs/SAI").expanduser()

DEFAULT_SCORED_ROOT     = _STATE_DIR / "rl" / "scored"
DEFAULT_TRAJECTORY_ROOT = _LOGS_DIR / "trajectories"


def _load_scored(root: Path, workflow: str | None) -> list[ScoredTrajectory]:
    store = ScoredTrajectoryStore(root=root)
    if not root.exists():
        return []
    if workflow:
        return store.read_all(workflow)
    return store.read_all_workflows()


def _load_raw(root: Path, workflow: str | None) -> list[RawTrajectory]:
    store = TrajectoryStore(root=root)
    if not root.exists():
        return []
    if workflow:
        return store.read_all(workflow)
    workflows = {p.stem for p in root.glob("*.jsonl")}
    out: list[RawTrajectory] = []
    for wf in workflows:
        out.extend(store.read_all(wf))
    return out


def _filter(
    trajectories: list[ScoredTrajectory],
    *,
    source: str | None,
    min_reward: float | None,
    max_reward: float | None,
) -> list[ScoredTrajectory]:
    if source:
        trajectories = [t for t in trajectories if t.reward_source == source]
    if min_reward is not None:
        trajectories = [t for t in trajectories if t.reward >= min_reward]
    if max_reward is not None:
        trajectories = [t for t in trajectories if t.reward <= max_reward]
    return trajectories


def _reward_bar(reward: float, width: int = 20) -> str:
    """ASCII bar: negative left, positive right, zero center."""
    center = width // 2
    if reward > 0:
        filled = round(reward * center)
        return " " * center + "█" * filled + " " * (center - filled)
    elif reward < 0:
        filled = round(abs(reward) * center)
        return " " * (center - filled) + "█" * filled + " " * center
    else:
        return " " * center + "│" + " " * (center - 1)


def _bucket(reward: float) -> str:
    if reward >= 1.0:  return "+1.0"
    if reward >= 0.3:  return "+0.3"
    if reward >  0.0:  return "+low"
    if reward == 0.0:  return " 0.0"
    if reward >= -0.8: return "-0.8"
    return "-1.0"


def _source_counts(trajectories: list[ScoredTrajectory]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in trajectories:
        counts[t.reward_source] = counts.get(t.reward_source, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _workflow_counts(trajectories: list[ScoredTrajectory]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in trajectories:
        counts[t.workflow_id] = counts.get(t.workflow_id, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def render_text(
    scored: list[ScoredTrajectory],
    raw: list[RawTrajectory],
    pairs_count: int,
    *,
    tail: int | None,
    show_pairs: bool,
    min_reward_gap: float,
) -> str:
    lines: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines += [
        f"┌{'─'*58}┐",
        f"│  SAI RL Report  —  {now:<38}│",
        f"└{'─'*58}┘",
        "",
    ]

    # ── overview ──────────────────────────────────────────────────────────
    lines += [
        f"  Scored trajectories : {len(scored)}",
        f"  Raw trajectories    : {len(raw)}",
        f"  Preference pairs    : {pairs_count}  (gap ≥ {min_reward_gap})",
        "",
    ]

    if not scored:
        lines.append("  (no scored trajectories found)")
        return "\n".join(lines)

    # ── reward distribution ───────────────────────────────────────────────
    bucket_counts: dict[str, int] = {}
    for t in scored:
        b = _bucket(t.reward)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    lines.append("  Reward distribution:")
    for b in ["+1.0", "+0.3", "+low", " 0.0", "-0.8", "-1.0"]:
        n = bucket_counts.get(b, 0)
        bar = "█" * n
        lines.append(f"    {b}  {bar:<30} {n}")
    lines.append("")

    # ── by reward source ──────────────────────────────────────────────────
    lines.append("  By reward source:")
    for src, n in _source_counts(scored).items():
        lines.append(f"    {src:<28} {n:>4}")
    lines.append("")

    # ── by workflow ───────────────────────────────────────────────────────
    wf_counts = _workflow_counts(scored)
    if len(wf_counts) > 1:
        lines.append("  By workflow:")
        for wf, n in wf_counts.items():
            lines.append(f"    {wf:<36} {n:>4}")
        lines.append("")

    # ── model usage ───────────────────────────────────────────────────────
    model_counts: dict[str, int] = {}
    for t in scored:
        model_counts[t.model_used] = model_counts.get(t.model_used, 0) + 1
    lines.append("  By model:")
    for model, n in sorted(model_counts.items(), key=lambda x: -x[1]):
        lines.append(f"    {model:<36} {n:>4}")
    lines.append("")

    # ── total cost ────────────────────────────────────────────────────────
    total_cost = sum(t.cost_usd for t in scored)
    lines.append(f"  Total inference cost: ${total_cost:.4f}")
    lines.append("")

    # ── tail ──────────────────────────────────────────────────────────────
    if tail:
        recent = sorted(scored, key=lambda t: t.scored_at)[-tail:]
        lines.append(f"  Last {tail} scored trajectories:")
        lines.append(f"  {'trajectory_id':<38} {'reward':>6}  {'source':<24} workflow")
        lines.append(f"  {'─'*38} {'─'*6}  {'─'*24} {'─'*20}")
        for t in recent:
            lines.append(
                f"  {t.trajectory_id:<38} {t.reward:>+6.1f}  "
                f"{t.reward_source:<24} {t.workflow_id}"
            )
        lines.append("")

    # ── preference pairs detail ───────────────────────────────────────────
    if show_pairs and pairs_count > 0:
        builder = PreferencePairBuilder(min_reward_gap=min_reward_gap)
        pairs = builder.build_pairs(scored)
        lines.append(f"  Preference pairs  (gap ≥ {min_reward_gap}):")
        lines.append(f"  {'pair_id':<38} {'chosen':>6}  {'rejected':>8}  gap")
        lines.append(f"  {'─'*38} {'─'*6}  {'─'*8}  {'─'*6}")
        for p in pairs:
            gap = p.chosen_reward - p.rejected_reward
            lines.append(
                f"  {p.pair_id:<38} {p.chosen_reward:>+6.1f}  "
                f"{p.rejected_reward:>+8.1f}  {gap:>+6.1f}"
            )
            lines.append(f"    chosen:   {p.chosen_response[:72]!r}")
            lines.append(f"    rejected: {p.rejected_response[:72]!r}")
        lines.append("")

    return "\n".join(lines)


def render_json(
    scored: list[ScoredTrajectory],
    raw: list[RawTrajectory],
    pairs_count: int,
) -> str:
    out: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scored_count": len(scored),
        "raw_count": len(raw),
        "pairs_count": pairs_count,
        "reward_distribution": {},
        "by_source": _source_counts(scored),
        "by_workflow": _workflow_counts(scored),
        "trajectories": [
            {
                "trajectory_id": t.trajectory_id,
                "invocation_id": t.invocation_id,
                "workflow_id": t.workflow_id,
                "reward": t.reward,
                "reward_source": t.reward_source,
                "human_actor": t.human_actor,
                "model_used": t.model_used,
                "cost_usd": t.cost_usd,
                "n_steps": len(t.steps),
                "terminated_reason": t.terminated_reason,
                "scored_at": t.scored_at.isoformat(),
                "user_message": t.user_message[:200],
                "final_response": t.final_response[:200],
            }
            for t in sorted(scored, key=lambda t: t.scored_at)
        ],
    }
    for t in scored:
        b = _bucket(t.reward)
        out["reward_distribution"][b] = out["reward_distribution"].get(b, 0) + 1
    return json.dumps(out, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query the SAI RL-from-human-feedback stores.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scored-root", type=Path, default=DEFAULT_SCORED_ROOT,
                        help=f"ScoredTrajectoryStore root (default: {DEFAULT_SCORED_ROOT})")
    parser.add_argument("--traj-root", type=Path, default=DEFAULT_TRAJECTORY_ROOT,
                        help=f"TrajectoryStore root (default: {DEFAULT_TRAJECTORY_ROOT})")
    parser.add_argument("--workflow", "-w", help="Filter to one workflow_id")
    parser.add_argument("--source", "-s",
                        choices=[r.value for r in RewardSource],
                        help="Filter by reward source")
    parser.add_argument("--min-reward", type=float, metavar="F",
                        help="Only show trajectories with reward >= F")
    parser.add_argument("--max-reward", type=float, metavar="F",
                        help="Only show trajectories with reward <= F")
    parser.add_argument("--tail", "-n", type=int, metavar="N",
                        help="Show last N scored trajectories")
    parser.add_argument("--pairs", "-p", action="store_true",
                        help="Show preference pairs detail")
    parser.add_argument("--gap", type=float, default=0.5, metavar="F",
                        help="Min reward gap for preference pairs (default: 0.5)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Machine-readable JSON output")
    args = parser.parse_args()

    scored = _load_scored(args.scored_root, args.workflow)
    raw    = _load_raw(args.traj_root, args.workflow)

    scored = _filter(
        scored,
        source=args.source,
        min_reward=args.min_reward,
        max_reward=args.max_reward,
    )

    builder = PreferencePairBuilder(min_reward_gap=args.gap)
    pairs_count = len(builder.build_pairs(scored))

    if args.as_json:
        print(render_json(scored, raw, pairs_count))
    else:
        print(render_text(
            scored, raw, pairs_count,
            tail=args.tail,
            show_pairs=args.pairs,
            min_reward_gap=args.gap,
        ))


if __name__ == "__main__":
    main()
