"""Transparent likelihood scoring for meeting decisions."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.meeting_models import MeetingAction, MeetingEvidence, MeetingLikelihoodAssessment


class MeetingLikelihoodPredictorTool:
    """Score whether Lutz should take a meeting using explicit weights."""

    def __init__(self, *, tool_id: str, predictor_config: dict[str, Any]) -> None:
        self.tool_id = tool_id
        self.predictor_config = predictor_config

    def assess(
        self,
        *,
        evidence: MeetingEvidence,
    ) -> tuple[MeetingLikelihoodAssessment, ToolExecutionRecord]:
        weights = self.predictor_config.get("weights", {})
        thresholds = self.predictor_config.get("thresholds", {})
        ask_for_more_types = {
            str(item) for item in self.predictor_config.get("ask_for_more_request_types", [])
        }
        manual_review_types = {
            str(item) for item in self.predictor_config.get("manual_review_request_types", [])
        }

        gmail_history = evidence.gmail_history
        calendar_history = evidence.calendar_history
        linkedin = evidence.linkedin

        score = float(thresholds.get("base_score", 0.35))
        reasons: list[str] = []

        if bool(gmail_history.get("has_prior_contact")):
            score += float(weights.get("prior_contact", 0.18))
            reasons.append("You have prior Gmail contact with this person.")
        if evidence.met_before_in_last_12_months or int(
            calendar_history.get("prior_meeting_count", 0)
        ) > 0:
            score += float(weights.get("prior_meeting", 0.22))
            reasons.append("You have met this contact before in calendar history.")
        if bool(linkedin.get("matched")):
            score += float(weights.get("linkedin_match", 0.12))
            reasons.append("This person appears in your LinkedIn dataset.")

        type_weights = self.predictor_config.get("request_type_weights", {})
        score += float(type_weights.get(evidence.request_type, 0.0))
        if evidence.request_type != "unknown":
            reasons.append(f"The request looks like a {evidence.request_type.replace('_', ' ')}.")

        decision: MeetingAction
        if evidence.request_type in manual_review_types:
            decision = "manual_review"
            reasons.append("This request type defaults to manual review.")
        elif evidence.request_type in ask_for_more_types:
            decision = "ask_for_more_info"
            reasons.append("This request type usually needs more detail before committing.")
        else:
            send_threshold = float(thresholds.get("send_calendar_link", 0.72))
            more_info_threshold = float(thresholds.get("ask_for_more_info", 0.48))
            if score >= send_threshold:
                decision = "send_calendar_link"
                reasons.append("The score is high enough to offer your calendar link.")
            elif score >= more_info_threshold:
                decision = "ask_for_more_info"
                reasons.append("The signal is promising but incomplete, so more info is safer.")
            else:
                decision = "manual_review"
                reasons.append("The signal is too weak for an automated draft.")

        confidence = min(max(score, 0.0), 0.98)
        # Phase 1 now drafts even uncertain/manual-review cases so Lutz can
        # see the reasoning and edit from a concrete starting point instead of
        # losing the candidate in a later review pass.
        should_create_draft = True
        assessment = MeetingLikelihoodAssessment(
            message_id=evidence.message_id,
            request_type=evidence.request_type,
            decision=decision,
            likelihood_score=round(min(max(score, 0.0), 1.0), 2),
            confidence=round(confidence, 2),
            reasons=reasons,
            should_create_draft=should_create_draft,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="meeting_likelihood_predictor",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "decision": assessment.decision,
                "likelihood_score": assessment.likelihood_score,
                "confidence": assessment.confidence,
                "reason_count": len(reasons),
            },
        )
        return assessment, record
