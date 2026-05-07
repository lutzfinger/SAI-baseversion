"""Dry run for the app/rl/ framework.

Exercises the full pipeline end-to-end with synthetic data:
  TrajectoryStore → HumanRewardScorer → PreferencePairBuilder → HuggingFaceExporter

No LLM calls, no external services, no real data required.
Output lands in /tmp/sai_rl_dry_run/.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# ── ensure repo root is on sys.path ────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.approvals.models import ApprovalRequest
from app.control_plane.slack_models import SlackFeedbackRecord
from app.eval.record import EvalRecord, ObservedReality, RealitySource
from app.rl.exporters import HuggingFaceExporter
from app.rl.models import RewardSource, ScoredTrajectory, TrajectoryStep
from app.rl.preference_pairs import PreferencePairBuilder
from app.rl.reward import HumanRewardScorer
from app.rl.trajectory import RawTrajectory, ScoredTrajectoryStore, TrajectoryStore
from app.shared.models import ApprovalStatus

NOW = datetime.now(UTC)
OUT = Path(tempfile.mkdtemp(prefix="sai_rl_dry_run_"))

print(f"\n{'='*60}")
print("  SAI RL Framework — Dry Run")
print(f"  Output: {OUT}")
print(f"{'='*60}\n")


# ── 1. Write synthetic trajectories ────────────────────────────────────────
print("── Phase 1: Trajectory capture ──────────────────────────────")

traj_store = TrajectoryStore(root=OUT / "trajectories")

trajectories = [
    RawTrajectory(
        invocation_id="inv_001",
        workflow_id="sai-email-interaction",
        system_prompt="You are SAI, a personal AI assistant. Triage email and suggest replies.",
        user_message="Subject: Invoice #1042 — please review\n\nHi, attached is invoice #1042 for last month.",
        steps=[
            TrajectoryStep(
                tool_name="search_gmail",
                args='{"query": "invoice #1042", "limit": 5}',
                result='[{"id": "msg_abc", "subject": "Invoice #1042", "from": "billing@vendor.com"}]',
                at=NOW,
            ),
            TrajectoryStep(
                tool_name="get_thread",
                args='{"thread_id": "thread_xyz"}',
                result='{"messages": 1, "body": "Please review invoice #1042 for $3,200."}',
                at=NOW,
                latency_ms=210,
            ),
        ],
        final_response="I've reviewed invoice #1042 for $3,200 from billing@vendor.com. I'll draft a confirmation reply acknowledging receipt and flagging it for your finance review.",
        model_used="claude-haiku-4-5",
        cost_usd=0.0023,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
    ),
    RawTrajectory(
        invocation_id="inv_002",
        workflow_id="sai-email-interaction",
        system_prompt="You are SAI, a personal AI assistant. Triage email and suggest replies.",
        user_message="Subject: Invoice #1042 — please review\n\nHi, attached is invoice #1042 for last month.",
        steps=[
            TrajectoryStep(
                tool_name="search_gmail",
                args='{"query": "invoice", "limit": 20}',
                result='[{"id": "msg_abc"}, {"id": "msg_def"}, {"id": "msg_ghi"}]',
                at=NOW,
            ),
        ],
        final_response="I found several invoices. Could you clarify which one you mean?",
        model_used="claude-haiku-4-5",
        cost_usd=0.0011,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
    ),
    RawTrajectory(
        invocation_id="inv_003",
        workflow_id="newsletter-identification",
        system_prompt="You are SAI. Classify emails as newsletter or not-newsletter.",
        user_message="Subject: Your weekly digest from TechCrunch\n\nHere are this week's top stories...",
        steps=[],
        final_response="newsletter",
        model_used="qwen2.5:7b",
        cost_usd=0.0,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
    ),
    RawTrajectory(
        invocation_id="inv_004",
        workflow_id="newsletter-identification",
        system_prompt="You are SAI. Classify emails as newsletter or not-newsletter.",
        user_message="Subject: Your weekly digest from TechCrunch\n\nHere are this week's top stories...",
        steps=[],
        final_response="not-newsletter",
        model_used="qwen2.5:7b",
        cost_usd=0.0,
        started_at=NOW,
        completed_at=NOW,
        terminated_reason="end_turn",
    ),
]

for t in trajectories:
    traj_store.append(t)
    print(f"  wrote trajectory {t.invocation_id}  [{t.workflow_id}]  steps={len(t.steps)}")

print(f"\n  Total stored: {sum(len(traj_store.read_all(wf)) for wf in ['sai-email-interaction', 'newsletter-identification'])}")


# ── 2. Score via HumanRewardScorer ────────────────────────────────────────
print("\n── Phase 2: Reward scoring ───────────────────────────────────")

scorer = HumanRewardScorer()

# Signal 1: operator approved inv_001 via approval gate
approval = ApprovalRequest(
    request_id="req_001",
    run_id="run_001",
    workflow_id="sai-email-interaction",
    action="send_reply",
    status=ApprovalStatus.APPROVED,
    requested_by="sai-system",
    requested_at=NOW,
    decided_at=NOW,
    decided_by="nathanael",
)
sig1 = scorer.score_from_approval(approval, trajectory_id=trajectories[0].trajectory_id)
print(f"  inv_001 ← approval APPROVED  → reward={sig1.scalar:+.1f}  source={sig1.source}")

# Signal 2: operator denied inv_002 via Slack button
feedback_deny = SlackFeedbackRecord(
    feedback_id="fb_001",
    slack_user_id="U_NATHANAEL",
    channel_id="C_SAI_EVAL",
    thread_ts="111.000",
    message_ts="111.001",
    feedback_type="action",
    action_id="reject",
    created_at=NOW,
)
sig2 = scorer.score_from_slack_feedback(feedback_deny, trajectory_id=trajectories[1].trajectory_id)
print(f"  inv_002 ← slack action=reject → reward={sig2.scalar:+.1f}  source={sig2.source}")

# Signal 3: eval record — newsletter correctly classified
record_correct = EvalRecord(
    task_id="newsletter-identification",
    input_id="msg_newsletter_001",
    input={"subject": "Your weekly digest from TechCrunch"},
    active_decision={"label": "newsletter"},
    decided_at=NOW,
)
record_correct.record_reality(ObservedReality(
    label={"label": "newsletter"},
    source=RealitySource.HUMAN_LABEL,
    observed_at=NOW,
))
sig3 = scorer.score_from_eval_record(record_correct, trajectory_id=trajectories[2].trajectory_id)
print(f"  inv_003 ← eval reality match  → reward={sig3.scalar:+.1f}  source={sig3.source}")

# Signal 4: eval record — wrong classification
record_wrong = EvalRecord(
    task_id="newsletter-identification",
    input_id="msg_newsletter_001",
    input={"subject": "Your weekly digest from TechCrunch"},
    active_decision={"label": "not-newsletter"},
    decided_at=NOW,
)
record_wrong.record_reality(ObservedReality(
    label={"label": "newsletter"},
    source=RealitySource.HUMAN_LABEL,
    observed_at=NOW,
))
sig4 = scorer.score_from_eval_record(record_wrong, trajectory_id=trajectories[3].trajectory_id)
print(f"  inv_004 ← eval reality mismatch → reward={sig4.scalar:+.1f}  source={sig4.source}")


# ── 3. Build ScoredTrajectory objects ─────────────────────────────────────
print("\n── Phase 3: Assembling ScoredTrajectories ────────────────────")

scored_store = ScoredTrajectoryStore(root=OUT / "scored")

scored = []
for traj, sig in zip(trajectories, [sig1, sig2, sig3, sig4]):
    st = ScoredTrajectory(
        trajectory_id=traj.trajectory_id,
        invocation_id=traj.invocation_id,
        workflow_id=traj.workflow_id,
        system_prompt=traj.system_prompt,
        user_message=traj.user_message,
        steps=traj.steps,
        final_response=traj.final_response,
        model_used=traj.model_used,
        cost_usd=traj.cost_usd,
        started_at=traj.started_at,
        completed_at=traj.completed_at,
        terminated_reason=traj.terminated_reason,
        reward=sig.scalar,
        reward_source=sig.source,
        human_actor=sig.decided_by,
        scored_at=NOW,
    )
    scored_store.append(st)
    scored.append(st)
    print(f"  {traj.invocation_id}  reward={sig.scalar:+.1f}  ({sig.source})")


# ── 4. Build preference pairs ─────────────────────────────────────────────
print("\n── Phase 4: Preference pairs (DPO) ──────────────────────────")

builder = PreferencePairBuilder(min_reward_gap=0.5)
pairs = builder.build_pairs(scored)

if pairs:
    for p in pairs:
        gap = p.chosen_reward - p.rejected_reward
        print(f"  pair: chosen={p.chosen_reward:+.1f} rejected={p.rejected_reward:+.1f} gap={gap:+.1f}")
        print(f"    chosen:   {p.chosen_response[:70]!r}")
        print(f"    rejected: {p.rejected_response[:70]!r}")
else:
    print("  (no pairs — different prompts or gap too small)")


# ── 5. Export all three formats ───────────────────────────────────────────
print("\n── Phase 5: HuggingFace export ──────────────────────────────")

exporter = HuggingFaceExporter()
export_dir = OUT / "export"

n_dpo = exporter.export_dpo(pairs, export_dir / "dpo_pairs.jsonl")
n_sft = exporter.export_sft(scored, export_dir / "sft.jsonl", min_reward=0.3)
n_scored = exporter.export_scored(scored, export_dir / "scored_ppo.jsonl")

print(f"  DPO    → {export_dir}/dpo_pairs.jsonl  ({n_dpo} rows)")
print(f"  SFT    → {export_dir}/sft.jsonl         ({n_sft} rows, min_reward=0.3)")
print(f"  Scored → {export_dir}/scored_ppo.jsonl  ({n_scored} rows)")

# Peek at one row of each
print("\n── Sample output ─────────────────────────────────────────────")
for label, path in [("DPO", export_dir / "dpo_pairs.jsonl"),
                     ("SFT", export_dir / "sft.jsonl"),
                     ("Scored", export_dir / "scored_ppo.jsonl")]:
    if path.exists():
        first = json.loads(path.read_text().splitlines()[0])
        keys = list(first.keys())
        print(f"\n  [{label}] keys: {keys}")
        if label == "DPO":
            print(f"    prompt:   {first['prompt'][:60]!r}")
            print(f"    chosen:   {first['chosen'][:60]!r}")
            print(f"    rejected: {first['rejected'][:60]!r}")
        elif label == "SFT":
            roles = [m["role"] for m in first["messages"]]
            print(f"    roles:   {roles}")
            print(f"    system:  {first['system'][:60]!r}")
        elif label == "Scored":
            print(f"    reward:  {first['reward']}")
            print(f"    prompt:  {first['prompt'][:60]!r}")
    else:
        print(f"\n  [{label}] no output (0 rows)")

print(f"\n{'='*60}")
print("  Dry run complete. All phases passed.")
print(f"  Files written to: {OUT}")
print(f"{'='*60}\n")
