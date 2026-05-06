"""Tests for HumanRewardScorer — all signals, no external dependencies."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.approvals.models import ApprovalRequest
from app.control_plane.slack_models import SlackFeedbackRecord
from app.eval.record import EvalRecord, ObservedReality, RealitySource
from app.rl.models import RewardSource
from app.rl.reward import HumanRewardScorer
from app.shared.models import ApprovalStatus

SCORER = HumanRewardScorer()
NOW = datetime.now(UTC)
TRAJ_ID = "traj_test_001"


def _approval(status: ApprovalStatus) -> ApprovalRequest:
    return ApprovalRequest(
        request_id="req_1",
        run_id="run_1",
        workflow_id="test-wf",
        action="send_email",
        status=status,
        requested_by="system",
        requested_at=NOW,
        decided_at=NOW,
        decided_by="nathanael",
    )


def _feedback(action_id: str | None = None, value: str | None = None, text: str | None = None) -> SlackFeedbackRecord:
    return SlackFeedbackRecord(
        feedback_id="fb_1",
        slack_user_id="U123",
        channel_id="C456",
        thread_ts="111.222",
        message_ts="111.333",
        feedback_type="action",
        action_id=action_id,
        value=value,
        text=text,
        created_at=NOW,
    )


def _eval_record(active: dict, reality: dict | None, is_gt: bool = True) -> EvalRecord:
    record = EvalRecord(
        task_id="task_1",
        input_id="input_1",
        input={"text": "test"},
        active_decision=active,
        decided_at=NOW,
        is_ground_truth=is_gt,
    )
    if reality is not None:
        record.record_reality(ObservedReality(
            label=reality,
            source=RealitySource.SLACK_ASK,
            observed_at=NOW,
        ))
    return record


class TestApprovalScoring:
    def test_approved_gives_plus_one(self):
        sig = SCORER.score_from_approval(_approval(ApprovalStatus.APPROVED), trajectory_id=TRAJ_ID)
        assert sig.scalar == 1.0
        assert sig.source == RewardSource.APPROVAL_APPROVED

    def test_denied_gives_minus_one(self):
        sig = SCORER.score_from_approval(_approval(ApprovalStatus.DENIED), trajectory_id=TRAJ_ID)
        assert sig.scalar == -1.0
        assert sig.source == RewardSource.APPROVAL_DENIED

    def test_pending_abstains(self):
        sig = SCORER.score_from_approval(_approval(ApprovalStatus.PENDING), trajectory_id=TRAJ_ID)
        assert sig.scalar == 0.0
        assert sig.source == RewardSource.ABSTAIN

    def test_decided_by_propagated(self):
        sig = SCORER.score_from_approval(_approval(ApprovalStatus.APPROVED), trajectory_id=TRAJ_ID)
        assert sig.decided_by == "nathanael"


class TestSlackFeedbackScoring:
    def test_approve_action_id(self):
        sig = SCORER.score_from_slack_feedback(_feedback(action_id="approve"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.SLACK_POSITIVE
        assert sig.scalar == 1.0

    def test_reject_action_id(self):
        sig = SCORER.score_from_slack_feedback(_feedback(action_id="reject"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.SLACK_NEGATIVE
        assert sig.scalar == -1.0

    def test_checkmark_emoji(self):
        sig = SCORER.score_from_slack_feedback(_feedback(value="✅"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.SLACK_POSITIVE

    def test_x_emoji(self):
        sig = SCORER.score_from_slack_feedback(_feedback(value="❌"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.SLACK_NEGATIVE

    def test_edit_text(self):
        sig = SCORER.score_from_slack_feedback(_feedback(text="please edit this reply"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.SLACK_EDIT
        assert sig.scalar == pytest.approx(0.3)

    def test_unknown_signal_abstains(self):
        sig = SCORER.score_from_slack_feedback(_feedback(text="thanks"), trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.ABSTAIN


class TestEvalRecordScoring:
    def test_matching_reality_gives_plus_one(self):
        record = _eval_record(active={"label": "newsletter"}, reality={"label": "newsletter"})
        sig = SCORER.score_from_eval_record(record, trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.EVAL_GROUND_TRUTH
        assert sig.scalar == 1.0

    def test_mismatched_reality_gives_minus_point_eight(self):
        record = _eval_record(active={"label": "newsletter"}, reality={"label": "work"})
        sig = SCORER.score_from_eval_record(record, trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.EVAL_MISMATCH
        assert sig.scalar == pytest.approx(-0.8)

    def test_no_ground_truth_abstains(self):
        record = _eval_record(active={"label": "newsletter"}, reality=None, is_gt=False)
        sig = SCORER.score_from_eval_record(record, trajectory_id=TRAJ_ID)
        assert sig.source == RewardSource.ABSTAIN

    def test_match_metadata_captured(self):
        record = _eval_record(active={"label": "work"}, reality={"label": "work"})
        sig = SCORER.score_from_eval_record(record, trajectory_id=TRAJ_ID)
        assert sig.metadata["match"] is True
