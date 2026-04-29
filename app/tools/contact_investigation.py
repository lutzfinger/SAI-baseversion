"""Deterministic deeper relationship analysis for ambiguous personal emails."""

from __future__ import annotations

from typing import Any

from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.personal_relationship_routing import (
    DetectDirectAddressInput,
    DetectDirectAddressTool,
    OtherToPersonalDecisionEngine,
    RelationshipDecisionInput,
    RelationshipRoutingEmail,
    RelationshipSignal,
    SummarizeRelationshipSignalsOutput,
)
from app.workers.contact_investigation_models import (
    ContactInvestigationAssessment,
    KnownRelationship,
)
from app.workers.email_models import EmailClassification, EmailMessage


class ContactRelationshipAnalyzerTool:
    """Use relationship evidence to decide whether `Other` should become `Personal`."""

    def __init__(
        self,
        *,
        tool_id: str,
        analyzer_config: dict[str, Any],
    ) -> None:
        self.tool_id = tool_id
        self.analyzer_config = analyzer_config
        self.direct_address_tool = DetectDirectAddressTool()
        self.decision_engine = OtherToPersonalDecisionEngine()

    def assess(
        self,
        *,
        message: EmailMessage,
        email_classification: EmailClassification,
        gmail_history: dict[str, Any],
        calendar_history: dict[str, Any],
        linkedin: dict[str, Any],
    ) -> tuple[ContactInvestigationAssessment, ToolExecutionRecord]:
        original_level1 = email_classification.level1_classification
        suggested = original_level1
        reasons: list[str] = []
        evidence_sources: list[str] = []

        prior_total_count = _safe_int(gmail_history.get("prior_total_count"))
        prior_outbound_count = _safe_int(gmail_history.get("prior_outbound_count"))
        prior_meeting_count = _safe_int(calendar_history.get("prior_meeting_count"))
        linkedin_matched = bool(linkedin.get("matched"))
        has_prior_contact = bool(gmail_history.get("has_prior_contact")) or (
            prior_total_count > 0 or prior_outbound_count > 0
        )
        direct_address = self.direct_address_tool.detect(
            DetectDirectAddressInput(
                subject=message.subject,
                body=message.body_excerpt or message.snippet,
                my_names=["Lutz", "Lutz Finger"],
            )
        )

        if has_prior_contact:
            reasons.append(
                "You have prior Gmail contact with this sender."
            )
            evidence_sources.append("gmail_history")
        if prior_meeting_count > 0:
            reasons.append("You have calendar history with this person in the last 12 months.")
            evidence_sources.append("calendar_history")
        if linkedin_matched:
            reasons.append("This sender matched your LinkedIn dataset.")
            evidence_sources.append("linkedin")
        if direct_address.directly_addresses_me:
            reasons.append("The message directly addresses Lutz.")
            evidence_sources.append("direct_address")

        relationship_summary = self._build_relationship_summary(
            has_prior_contact=has_prior_contact,
            prior_meeting_count=prior_meeting_count,
            linkedin_matched=linkedin_matched,
            direct_address=direct_address,
        )
        decision = self.decision_engine.decide(
            RelationshipDecisionInput(
                existing_category=original_level1,
                relationship_summary=relationship_summary,
                email=RelationshipRoutingEmail(
                    sender_name=message.from_name,
                    sender_email=message.from_email,
                    recipients=list(message.to),
                    cc=list(message.cc),
                    subject=message.subject,
                    body=message.body_excerpt or message.snippet,
                ),
            )
        )
        relationship = self._relationship_type(
            has_prior_contact=has_prior_contact,
            prior_meeting_count=prior_meeting_count,
            linkedin_matched=linkedin_matched,
        )

        if decision.override_applied and decision.final_category == "L1/Personal":
            suggested = "personal"
        reasons.append(decision.human_explanation)

        category_updated = suggested != original_level1
        confidence = decision.confidence
        assessment = ContactInvestigationAssessment(
            message_id=message.message_id,
            original_level1_classification=original_level1,
            suggested_level1_classification=suggested,
            category_updated=category_updated,
            known_contact=has_prior_contact or prior_meeting_count > 0 or linkedin_matched,
            known_relationship=relationship,
            confidence=confidence,
            reasons=reasons or ["No cross-source evidence established a clearer relationship yet."],
            evidence_sources=sorted(set(evidence_sources)),
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="contact_relationship_analyzer",
            status=ToolExecutionStatus.COMPLETED,
            details={
                "original_level1_classification": original_level1,
                "suggested_level1_classification": suggested,
                "category_updated": category_updated,
                "known_relationship": relationship,
                "confidence": confidence,
                "reason_codes": decision.reason_codes,
            },
        )
        return assessment, record

    def _build_relationship_summary(
        self,
        *,
        has_prior_contact: bool,
        prior_meeting_count: int,
        linkedin_matched: bool,
        direct_address: object,
    ) -> SummarizeRelationshipSignalsOutput:
        signals: list[RelationshipSignal] = []
        if has_prior_contact:
            signals.append(
                RelationshipSignal(
                    signal="prior_email_history",
                    value=True,
                    confidence=min(
                        float(self.analyzer_config.get("base_confidence", 0.56))
                        + float(self.analyzer_config.get("prior_contact_bonus", 0.12)),
                        0.95,
                    ),
                    strength="strong",
                    reason_code="prior_email_history",
                )
            )
        if prior_meeting_count > 0:
            signals.append(
                RelationshipSignal(
                    signal="prior_meeting",
                    value=True,
                    confidence=min(
                        float(self.analyzer_config.get("base_confidence", 0.56))
                        + float(self.analyzer_config.get("prior_meeting_bonus", 0.16)),
                        0.98,
                    ),
                    strength="strong",
                    reason_code="prior_meeting",
                )
            )
        if linkedin_matched:
            signals.append(
                RelationshipSignal(
                    signal="linkedin_match",
                    value=True,
                    confidence=min(
                        float(self.analyzer_config.get("base_confidence", 0.56))
                        + float(self.analyzer_config.get("linkedin_bonus", 0.10)),
                        0.82,
                    ),
                    strength="weak",
                    reason_code="linkedin_match",
                )
            )
        if getattr(direct_address, "directly_addresses_me", False):
            signals.append(
                RelationshipSignal(
                    signal="directly_addresses_me",
                    value=True,
                    confidence=float(getattr(direct_address, "confidence", 0.96)),
                    strength="strong",
                    reason_code="direct_address",
                )
            )

        strong_count = sum(1 for signal in signals if signal.strength == "strong")
        weak_count = sum(1 for signal in signals if signal.strength == "weak")
        explanation = (
            "No relationship evidence found."
            if not signals
            else "Sender is known via " + ", ".join(signal.signal for signal in signals) + "."
        )
        return SummarizeRelationshipSignalsOutput(
            relationship_signals=signals,
            relationship_score=round(min(0.55 * strong_count + 0.3 * weak_count, 0.99), 2),
            has_relationship_evidence=bool(signals),
            strong_signal_count=strong_count,
            weak_signal_count=weak_count,
            explanation=explanation,
        )

    def _relationship_type(
        self,
        *,
        has_prior_contact: bool,
        prior_meeting_count: int,
        linkedin_matched: bool,
    ) -> KnownRelationship:
        if prior_meeting_count > 0:
            return "met_before"
        if has_prior_contact:
            return "known_contact"
        if linkedin_matched:
            return "linkedin_connection"
        return "unknown"


def _safe_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            return 0
    return 0
