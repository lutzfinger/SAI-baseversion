"""Convert SAI's human feedback signals into scalar reward signals."""

from __future__ import annotations

from datetime import UTC, datetime

from app.approvals.models import ApprovalRequest
from app.control_plane.slack_models import SlackFeedbackRecord
from app.eval.record import EvalRecord
from app.rl.models import REWARD_SCALARS, RewardSignal, RewardSource
from app.shared.models import ApprovalStatus

_POSITIVE_IDS = frozenset({"approve", "approved", "yes", "correct", "accept", "confirm"})
_NEGATIVE_IDS = frozenset({"reject", "rejected", "deny", "denied", "no", "incorrect"})
_EDIT_KEYWORDS = frozenset({"edit", "revise", "correct", "update", "change", "fix"})

# Slack emoji values stored by the bolt handler
_POSITIVE_EMOJI = frozenset({"✅", ":white_check_mark:", "+1", ":thumbsup:"})
_NEGATIVE_EMOJI = frozenset({"❌", ":x:", "-1", ":thumbsdown:"})


class HumanRewardScorer:
    """Maps SAI human feedback events to scalar reward signals.

    Three independent signal sources, in order of reliability:
      1. ApprovalRequest (most explicit — operator literally approved/denied)
      2. EvalRecord reality observation (ground truth confirmed by real-world action)
      3. SlackFeedbackRecord (button/emoji/text reply — weakest but most frequent)
    """

    def score_from_approval(
        self,
        request: ApprovalRequest,
        *,
        trajectory_id: str,
    ) -> RewardSignal:
        if request.status == ApprovalStatus.APPROVED:
            source = RewardSource.APPROVAL_APPROVED
        elif request.status == ApprovalStatus.DENIED:
            source = RewardSource.APPROVAL_DENIED
        else:
            source = RewardSource.ABSTAIN

        decided_at = request.decided_at or request.requested_at
        return RewardSignal(
            trajectory_id=trajectory_id,
            source=source,
            scalar=REWARD_SCALARS[source],
            decided_by=request.decided_by,
            decided_at=decided_at,
            metadata={
                "request_id": request.request_id,
                "action": request.action,
                "workflow_id": request.workflow_id,
            },
        )

    def score_from_eval_record(
        self,
        record: EvalRecord,
        *,
        trajectory_id: str,
    ) -> RewardSignal:
        if not record.is_ground_truth or record.reality is None:
            return RewardSignal(
                trajectory_id=trajectory_id,
                source=RewardSource.ABSTAIN,
                scalar=0.0,
                decided_at=datetime.now(UTC),
                metadata={"record_id": record.record_id, "reason": "no_ground_truth"},
            )

        match = record.active_decision == record.reality.label
        source = RewardSource.EVAL_GROUND_TRUTH if match else RewardSource.EVAL_MISMATCH
        return RewardSignal(
            trajectory_id=trajectory_id,
            source=source,
            scalar=REWARD_SCALARS[source],
            decided_at=record.reality.observed_at,
            metadata={
                "record_id": record.record_id,
                "reality_source": record.reality.source,
                "match": match,
                "active_decision": record.active_decision,
                "reality_label": record.reality.label,
            },
        )

    def score_from_slack_feedback(
        self,
        feedback: SlackFeedbackRecord,
        *,
        trajectory_id: str,
    ) -> RewardSignal:
        source = self._classify_slack(feedback)
        return RewardSignal(
            trajectory_id=trajectory_id,
            source=source,
            scalar=REWARD_SCALARS[source],
            decided_by=feedback.slack_user_id,
            decided_at=feedback.created_at,
            metadata={
                "feedback_id": feedback.feedback_id,
                "action_id": feedback.action_id,
                "value": feedback.value,
                "feedback_type": feedback.feedback_type,
            },
        )

    def _classify_slack(self, feedback: SlackFeedbackRecord) -> RewardSource:
        action_id = (feedback.action_id or "").lower().strip()
        value = (feedback.value or "").strip()
        text = (feedback.text or "").lower().strip()

        if action_id in _POSITIVE_IDS or value.lower() in _POSITIVE_IDS:
            return RewardSource.SLACK_POSITIVE
        if action_id in _NEGATIVE_IDS or value.lower() in _NEGATIVE_IDS:
            return RewardSource.SLACK_NEGATIVE
        if value in _POSITIVE_EMOJI:
            return RewardSource.SLACK_POSITIVE
        if value in _NEGATIVE_EMOJI:
            return RewardSource.SLACK_NEGATIVE
        if any(kw in text for kw in _EDIT_KEYWORDS):
            return RewardSource.SLACK_EDIT
        return RewardSource.ABSTAIN
