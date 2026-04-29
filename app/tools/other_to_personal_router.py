"""Apply deterministic Other->Personal overrides using relationship evidence."""

from __future__ import annotations

from typing import Any

from app.connectors.gmail_auth import GmailAuthConfigurationError, GmailOAuthAuthenticator
from app.connectors.gmail_history import GmailHistoryConnector
from app.learning.relationship_memory import RelationshipMemoryStore
from app.shared.config import Settings
from app.shared.models import PolicyDocument, PolicyMode, WorkflowToolDefinition
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.tools.personal_relationship_routing import (
    DetectDirectAddressInput,
    DetectDirectAddressTool,
    LinkedInCsvLookupTool,
    LookupLinkedinCsvInput,
    OtherToPersonalDecisionEngine,
    RelationshipDecisionInput,
    RelationshipRoutingEmail,
    RelationshipSignal,
    SummarizeRelationshipSignalsOutput,
)
from app.workers.email_models import EmailClassification, EmailMessage


class OtherToPersonalRouterTool:
    """Use local relationship evidence to upgrade ambiguous `Other` emails."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        settings: Settings,
        policy: PolicyDocument | None = None,
        history_connector: GmailHistoryConnector | None = None,
        linkedin_tool: LinkedInCsvLookupTool | None = None,
        my_names: list[str] | None = None,
    ) -> None:
        self.tool_definition = tool_definition
        self.settings = settings
        self.policy = policy
        self.memory = RelationshipMemoryStore(settings.relationship_memory_path)
        self.direct_address_tool = DetectDirectAddressTool()
        self.decision_engine = OtherToPersonalDecisionEngine()
        self.history_connector = history_connector or self._build_history_connector()
        self.linkedin_tool = linkedin_tool or LinkedInCsvLookupTool(
            dataset_path=settings.linkedin_dataset_path
        )
        self.my_names = my_names or ["Lutz", "Lutz Finger"]

    def apply(
        self,
        *,
        message: EmailMessage,
        classification: EmailClassification,
    ) -> tuple[EmailClassification, ToolExecutionRecord]:
        if classification.level1_classification != "other":
            return classification, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.SKIPPED,
                details={"reason": "classification_not_other"},
            )

        memory_entry = self.memory.lookup(message.from_email)
        direct_address = self.direct_address_tool.detect(
            DetectDirectAddressInput(
                subject=message.subject,
                body=message.body_excerpt or message.snippet,
                my_names=self.my_names,
            )
        )
        live_summary, live_details = self._lookup_live_relationship_summary(message=message)
        summary = _merge_signals(
            base_summary=(
                memory_entry.relationship_summary if memory_entry is not None else None
            ),
            live_summary=live_summary,
            direct_address=direct_address,
        )
        if not summary.has_relationship_evidence:
            return classification, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.SKIPPED,
                details={
                    "reason": "no_relationship_evidence",
                    "memory_hit": False,
                    "direct_address": direct_address.directly_addresses_me,
                    **live_details,
                },
            )

        decision = self.decision_engine.decide(
            RelationshipDecisionInput(
                existing_category=classification.level1_classification,
                relationship_summary=summary,
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
        if not decision.override_applied or decision.final_category != "L1/Personal":
            return classification, ToolExecutionRecord(
                tool_id=self.tool_definition.tool_id,
                tool_kind=self.tool_definition.kind,
                status=ToolExecutionStatus.COMPLETED,
                details={
                    "override_applied": False,
                    "memory_hit": memory_entry is not None,
                    "reason_codes": decision.reason_codes,
                    "human_explanation": decision.human_explanation,
                    "direct_address": direct_address.directly_addresses_me,
                    **live_details,
                    "source_message_id": (
                        memory_entry.source_message_id if memory_entry is not None else None
                    ),
                },
            )

        updated = classification.model_copy(
            update={
                "level1_classification": "personal",
                "confidence": max(classification.confidence, decision.confidence),
                "reason": (
                    f"{classification.reason} Routed to Personal because "
                    f"{decision.human_explanation}"
                )[:240],
            }
        )
        return updated, ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "override_applied": True,
                "memory_hit": memory_entry is not None,
                "reason_codes": decision.reason_codes,
                "human_explanation": decision.human_explanation,
                "direct_address": direct_address.directly_addresses_me,
                **live_details,
                "source_message_id": (
                    memory_entry.source_message_id if memory_entry is not None else None
                ),
                "source_workflow_id": (
                    memory_entry.source_workflow_id if memory_entry is not None else None
                ),
            },
        )

    def _build_history_connector(self) -> GmailHistoryConnector | None:
        if self.policy is None:
            return None
        if self.policy.mode_for("connector.gmail.read_history") != PolicyMode.ALLOW:
            return None
        try:
            return GmailHistoryConnector(
                authenticator=GmailOAuthAuthenticator(
                    settings=self.settings,
                    policy=self.policy,
                ),
                max_history_results=25,
            )
        except GmailAuthConfigurationError:
            return None

    def _lookup_live_relationship_summary(
        self,
        *,
        message: EmailMessage,
    ) -> tuple[SummarizeRelationshipSignalsOutput | None, dict[str, Any]]:
        signals: list[RelationshipSignal] = []
        details: dict[str, Any] = {
            "live_history_available": self.history_connector is not None,
            "live_history_contact": False,
            "live_history_prior_outbound_count": 0,
            "live_history_prior_total_count": 0,
            "live_meeting_signal": False,
            "live_meeting_signal_count": 0,
            "linkedin_match_found": False,
            "linkedin_match_type": "none",
            "linkedin_match_name": None,
        }

        if self.history_connector is not None:
            try:
                history_summary = self.history_connector.summarize_contact(
                    contact_email=message.from_email,
                    calendar_link=self.settings.meeting_calendar_link,
                )
                prior_outbound_count = _safe_int(history_summary.get("prior_outbound_count"))
                prior_total_count = _safe_int(history_summary.get("prior_total_count"))
                if prior_outbound_count > 0:
                    signals.append(
                        RelationshipSignal(
                            signal="prior_email_history",
                            value=True,
                            confidence=0.9,
                            strength="strong",
                            reason_code="prior_email_history",
                        )
                    )
                elif prior_total_count > 0:
                    signals.append(
                        RelationshipSignal(
                            signal="prior_inbound_history",
                            value=True,
                            confidence=0.66,
                            strength="weak",
                            reason_code="prior_inbound_history",
                        )
                    )
                if prior_outbound_count > 0 or prior_total_count > 0:
                    details["live_history_contact"] = True
                    details["live_history_prior_outbound_count"] = prior_outbound_count
                    details["live_history_prior_total_count"] = prior_total_count

                meeting_summary = self.history_connector.summarize_meeting_evidence(
                    contact_email=message.from_email,
                    contact_name=message.from_name,
                    lookback_days=self.settings.meeting_history_lookback_days,
                )
                prior_meeting_count = _safe_int(meeting_summary.get("prior_meeting_count"))
                if prior_meeting_count > 0:
                    signals.append(
                        RelationshipSignal(
                            signal="prior_meeting",
                            value=True,
                            confidence=0.96,
                            strength="strong",
                            reason_code="prior_meeting",
                        )
                    )
                    details["live_meeting_signal"] = True
                    details["live_meeting_signal_count"] = prior_meeting_count
            except Exception as exc:
                details["live_history_error"] = str(exc)

        if self.linkedin_tool is not None:
            linkedin_match = self.linkedin_tool.lookup(
                LookupLinkedinCsvInput(name=message.from_name or "")
            )
            if linkedin_match.found:
                signals.append(
                    RelationshipSignal(
                        signal="linkedin_match",
                        value=True,
                        confidence=linkedin_match.confidence,
                        strength="weak",
                        reason_code="linkedin_match",
                    )
                )
                details["linkedin_match_found"] = True
                details["linkedin_match_type"] = linkedin_match.match_type
                details["linkedin_match_name"] = linkedin_match.matched_name

        if not signals:
            return None, details

        strong_count = sum(1 for signal in signals if signal.strength == "strong")
        weak_count = sum(1 for signal in signals if signal.strength == "weak")
        return (
            SummarizeRelationshipSignalsOutput(
                relationship_signals=signals,
                relationship_score=round(min(0.55 * strong_count + 0.3 * weak_count, 0.99), 2),
                has_relationship_evidence=True,
                strong_signal_count=strong_count,
                weak_signal_count=weak_count,
                explanation="Sender is known via live relationship evidence.",
            ),
            details,
        )


def _merge_signals(
    *,
    base_summary: SummarizeRelationshipSignalsOutput | None,
    live_summary: SummarizeRelationshipSignalsOutput | None,
    direct_address: object,
) -> SummarizeRelationshipSignalsOutput:
    signals = list(base_summary.relationship_signals) if base_summary is not None else []
    if live_summary is not None:
        existing_codes = {signal.reason_code for signal in signals}
        for signal in live_summary.relationship_signals:
            if signal.reason_code not in existing_codes:
                signals.append(signal)
                existing_codes.add(signal.reason_code)
    if getattr(direct_address, "directly_addresses_me", False) and not any(
        signal.reason_code == "direct_address" for signal in signals
    ):
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


def _safe_int(value: object) -> int:
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
