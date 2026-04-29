"""Deterministic relationship-based routing from Other to Personal."""

from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field


class RelationshipRoutingEmail(BaseModel):
    """Normalized email payload used by the relationship-routing workflow."""

    model_config = ConfigDict(extra="forbid")

    sender_name: str | None = None
    sender_email: str
    recipients: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str


class LookupLinkedinCsvInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class LookupLinkedinCsvOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    matched_name: str | None
    match_type: Literal["exact", "fuzzy", "none"]
    confidence: float = Field(ge=0.0, le=1.0)
    error: str | None = None


class SearchSentEmailHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


class SearchSentEmailHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_written_before: bool
    times_written: int
    last_written_at: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    error: str | None = None


class SearchNonAutomatedRepliesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


class SearchNonAutomatedRepliesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_non_automated_reply: bool
    reply_count: int
    examples_found: int
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool = False
    error: str | None = None


class DetectKnownPersonMentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str


class DetectKnownPersonMentionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mentions_known_person: bool
    mentioned_names: list[str] = Field(default_factory=list)
    matched_known_names: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    candidate_names: list[str] = Field(default_factory=list)
    error: str | None = None


class CheckContactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    name: str | None = None


class CheckContactsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_contacts: bool
    matched_contact_name: str | None
    matched_contact_email: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    error: str | None = None


class CheckMeetingHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    name: str | None = None


class CheckMeetingHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    had_meeting_before: bool
    meeting_count: int
    last_meeting_at: str | None
    matched_as: Literal["email", "name", "none"]
    confidence: float = Field(ge=0.0, le=1.0)
    error: str | None = None


class DetectDirectAddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str
    my_names: list[str] = Field(default_factory=list)


class DetectDirectAddressOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directly_addresses_me: bool
    matched_name: str | None
    evidence_snippet: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class RelationshipSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: str
    value: bool
    confidence: float = Field(ge=0.0, le=1.0)
    strength: Literal["strong", "weak"]
    reason_code: str


class SummarizeRelationshipSignalsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linkedin_match: LookupLinkedinCsvOutput
    written_before: SearchSentEmailHistoryOutput
    non_automated_reply: SearchNonAutomatedRepliesOutput
    known_person_mention: DetectKnownPersonMentionOutput
    contacts_match: CheckContactsOutput
    meeting_history: CheckMeetingHistoryOutput
    direct_address: DetectDirectAddressOutput


class SummarizeRelationshipSignalsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_signals: list[RelationshipSignal] = Field(default_factory=list)
    relationship_score: float = Field(ge=0.0, le=1.0)
    has_relationship_evidence: bool
    strong_signal_count: int = 0
    weak_signal_count: int = 0
    explanation: str


class RelationshipDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    existing_category: str
    relationship_summary: SummarizeRelationshipSignalsOutput
    email: RelationshipRoutingEmail


class RelationshipDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_category: Literal["L1/Personal", "L1/Other", "unchanged"]
    override_applied: bool
    reason_codes: list[str] = Field(default_factory=list)
    human_explanation: str
    confidence: float = Field(ge=0.0, le=1.0)


class RelationshipRoutingWorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: RelationshipRoutingEmail
    existing_category: str
    linkedin_match: LookupLinkedinCsvOutput
    written_before: SearchSentEmailHistoryOutput
    non_automated_reply: SearchNonAutomatedRepliesOutput
    known_person_mention: DetectKnownPersonMentionOutput
    contacts_match: CheckContactsOutput
    meeting_history: CheckMeetingHistoryOutput
    direct_address: DetectDirectAddressOutput
    relationship_summary: SummarizeRelationshipSignalsOutput
    decision: RelationshipDecisionOutput


class SentMessageRecord(BaseModel):
    """Minimal sent-mail record used by deterministic relationship tools."""

    model_config = ConfigDict(extra="forbid")

    recipient_email: str
    sent_at: datetime | None = None
    subject: str = ""
    body: str = ""
    is_reply: bool = False
    is_automated: bool = False


class MeetingRecord(BaseModel):
    """Minimal meeting-history record used by deterministic relationship tools."""

    model_config = ConfigDict(extra="forbid")

    matched_as: Literal["email", "name"]
    happened_at: datetime | None = None


class ContactRecord(BaseModel):
    """Minimal contact-book record for local deterministic lookups."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None


class SentHistoryBackend(Protocol):
    def search_sent_messages(self, *, email: str) -> list[SentMessageRecord]:
        """Return sent messages addressed to one email."""


class MeetingHistoryBackend(Protocol):
    def search_meetings(
        self,
        *,
        email: str | None,
        name: str | None,
    ) -> list[MeetingRecord]:
        """Return prior meeting records for one person."""


class LinkedInCsvLookupTool:
    """Check a LinkedIn CSV export for exact or fuzzy familiarity by name."""

    def __init__(self, *, dataset_path: Path | None) -> None:
        self.dataset_path = dataset_path
        self._names: list[str] | None = None

    def lookup(self, request: LookupLinkedinCsvInput) -> LookupLinkedinCsvOutput:
        name = request.name.strip()
        if not name or self.dataset_path is None or not self.dataset_path.exists():
            return LookupLinkedinCsvOutput(
                found=False,
                matched_name=None,
                match_type="none",
                confidence=0.0,
                error="linkedin_csv_unavailable",
            )

        names = self._load_names()
        if not names:
            return LookupLinkedinCsvOutput(
                found=False,
                matched_name=None,
                match_type="none",
                confidence=0.0,
                error="linkedin_csv_unavailable",
            )

        exact = _exact_name_match(name=name, candidates=names)
        if exact is not None:
            return LookupLinkedinCsvOutput(
                found=True,
                matched_name=exact,
                match_type="exact",
                confidence=1.0,
            )

        fuzzy_match = _best_fuzzy_name_match(name=name, candidates=names)
        if fuzzy_match is None:
            return LookupLinkedinCsvOutput(
                found=False,
                matched_name=None,
                match_type="none",
                confidence=0.0,
            )

        matched_name, ratio, ambiguous = fuzzy_match
        if ambiguous or ratio < 0.9:
            return LookupLinkedinCsvOutput(
                found=False,
                matched_name=None,
                match_type="none",
                confidence=round(max(ratio - 0.1, 0.0), 2),
            )
        return LookupLinkedinCsvOutput(
            found=True,
            matched_name=matched_name,
            match_type="fuzzy",
            confidence=round(ratio, 2),
        )

    def known_names(self) -> list[str]:
        if self.dataset_path is None or not self.dataset_path.exists():
            return []
        return list(self._load_names())

    def _load_names(self) -> list[str]:
        if self._names is not None:
            return self._names
        dataset_path = self.dataset_path
        if dataset_path is None:
            self._names = []
            return self._names
        suffix = dataset_path.suffix.lower()
        if suffix == ".json":
            raw = json.loads(dataset_path.read_text(encoding="utf-8"))
            self._names = sorted(
                {
                    _clean_name(str(record.get("name", "")))
                    for record in raw
                    if isinstance(record, dict) and _clean_name(str(record.get("name", "")))
                }
            )
            return self._names

        names: set[str] = set()
        if suffix == ".csv":
            with dataset_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    direct_name = _clean_name(row.get("name", "") or row.get("Name", ""))
                    if direct_name:
                        names.add(direct_name)
                        continue
                    first_name = _clean_name(
                        row.get("First Name", "") or row.get("first_name", "")
                    )
                    last_name = _clean_name(
                        row.get("Last Name", "") or row.get("last_name", "")
                    )
                    combined = _clean_name(
                        " ".join(part for part in [first_name, last_name] if part)
                    )
                    if combined:
                        names.add(combined)
        self._names = sorted(names)
        return self._names


class SearchSentEmailHistoryTool:
    """Check whether Lutz has written to a sender before."""

    def __init__(self, *, backend: SentHistoryBackend | None) -> None:
        self.backend = backend

    def search(self, request: SearchSentEmailHistoryInput) -> SearchSentEmailHistoryOutput:
        if self.backend is None:
            return SearchSentEmailHistoryOutput(
                has_written_before=False,
                times_written=0,
                last_written_at=None,
                confidence=0.0,
                error="sent_email_history_unavailable",
            )

        messages = self.backend.search_sent_messages(email=request.email.lower())
        times_written = len(messages)
        last_written_at = _latest_timestamp(message.sent_at for message in messages)
        return SearchSentEmailHistoryOutput(
            has_written_before=times_written > 0,
            times_written=times_written,
            last_written_at=last_written_at,
            confidence=0.98 if times_written > 0 else 0.82,
        )


class SearchNonAutomatedRepliesTool:
    """Check whether Lutz has sent real non-automated replies before."""

    def __init__(self, *, backend: SentHistoryBackend | None) -> None:
        self.backend = backend

    def search(
        self,
        request: SearchNonAutomatedRepliesInput,
    ) -> SearchNonAutomatedRepliesOutput:
        if self.backend is None:
            return SearchNonAutomatedRepliesOutput(
                has_non_automated_reply=False,
                reply_count=0,
                examples_found=0,
                confidence=0.0,
                error="sent_email_history_unavailable",
            )

        messages = self.backend.search_sent_messages(email=request.email.lower())
        replies = [message for message in messages if _looks_like_non_automated_reply(message)]
        uncertain_count = sum(
            1
            for message in messages
            if not message.is_automated and not _looks_like_human_text(message)
        )
        confidence = 0.96 if replies else 0.76
        if uncertain_count:
            confidence = min(confidence, 0.72)
        return SearchNonAutomatedRepliesOutput(
            has_non_automated_reply=bool(replies),
            reply_count=len(replies),
            examples_found=min(len(replies), 2),
            confidence=round(confidence, 2),
            needs_review=uncertain_count > 0 and not replies,
        )


class DetectKnownPersonMentionTool:
    """Detect whether the message mentions someone already known to Lutz."""

    def __init__(self, *, known_names: list[str] | None) -> None:
        self.known_names = sorted({name for name in (known_names or []) if _clean_name(name)})

    def detect(
        self,
        request: DetectKnownPersonMentionInput,
    ) -> DetectKnownPersonMentionOutput:
        if not self.known_names:
            return DetectKnownPersonMentionOutput(
                mentions_known_person=False,
                mentioned_names=[],
                matched_known_names=[],
                confidence=0.0,
                error="known_people_index_unavailable",
            )

        fresh_text = _fresh_text(f"{request.subject}\n{request.body}")
        candidate_names = _extract_candidate_names(fresh_text)
        matched_known_names: list[str] = []
        ambiguous_candidates: list[str] = []

        for known_name in self.known_names:
            if _contains_name(fresh_text, known_name):
                matched_known_names.append(known_name)

        for candidate_name in candidate_names:
            fuzzy = _best_fuzzy_name_match(name=candidate_name, candidates=self.known_names)
            if fuzzy is None:
                continue
            matched_name, ratio, ambiguous = fuzzy
            if ambiguous:
                ambiguous_candidates.append(candidate_name)
                continue
            if ratio >= 0.94 and matched_name not in matched_known_names:
                matched_known_names.append(matched_name)

        return DetectKnownPersonMentionOutput(
            mentions_known_person=bool(matched_known_names),
            mentioned_names=sorted(candidate_names),
            matched_known_names=sorted(matched_known_names),
            confidence=0.84 if matched_known_names else 0.24,
            candidate_names=sorted(set(ambiguous_candidates)),
        )


class CheckContactsTool:
    """Check whether a sender exists in a local contacts dataset."""

    def __init__(self, *, dataset_path: Path | None) -> None:
        self.dataset_path = dataset_path
        self._records: list[ContactRecord] | None = None

    def check(self, request: CheckContactsInput) -> CheckContactsOutput:
        if self.dataset_path is None or not self.dataset_path.exists():
            return CheckContactsOutput(
                in_contacts=False,
                matched_contact_name=None,
                matched_contact_email=None,
                confidence=0.0,
                error="contacts_unavailable",
            )

        records = self._load_records()
        normalized_email = request.email.strip().lower()
        normalized_name = _normalized_name(request.name)

        for record in records:
            record_email = (record.email or "").strip().lower()
            if record_email and normalized_email and record_email == normalized_email:
                return CheckContactsOutput(
                    in_contacts=True,
                    matched_contact_name=record.name,
                    matched_contact_email=record.email,
                    confidence=0.99,
                )

        if normalized_name:
            exact = next(
                (
                    record
                    for record in records
                    if _normalized_name(record.name) == normalized_name
                ),
                None,
            )
            if exact is not None:
                return CheckContactsOutput(
                    in_contacts=True,
                    matched_contact_name=exact.name,
                    matched_contact_email=exact.email,
                    confidence=0.95,
                )

            name_candidates = [record.name or "" for record in records if record.name]
            fuzzy = _best_fuzzy_name_match(name=request.name or "", candidates=name_candidates)
            if fuzzy is not None:
                matched_name, ratio, ambiguous = fuzzy
                if not ambiguous and ratio >= 0.92:
                    matched = next(
                        record for record in records if (record.name or "") == matched_name
                    )
                    return CheckContactsOutput(
                        in_contacts=True,
                        matched_contact_name=matched.name,
                        matched_contact_email=matched.email,
                        confidence=round(ratio, 2),
                    )

        return CheckContactsOutput(
            in_contacts=False,
            matched_contact_name=None,
            matched_contact_email=None,
            confidence=0.78,
        )

    def known_names(self) -> list[str]:
        if self.dataset_path is None or not self.dataset_path.exists():
            return []
        return sorted({record.name for record in self._load_records() if record.name})

    def _load_records(self) -> list[ContactRecord]:
        if self._records is not None:
            return self._records
        dataset_path = self.dataset_path
        if dataset_path is None:
            self._records = []
            return self._records
        suffix = dataset_path.suffix.lower()
        rows: list[dict[str, Any]]
        if suffix == ".json":
            rows = cast(
                list[dict[str, Any]],
                json.loads(dataset_path.read_text(encoding="utf-8")),
            )
        else:
            with dataset_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self._records = [
            ContactRecord(
                name=_clean_name(str(row.get("name") or row.get("Name") or "")) or None,
                email=(
                    str(row.get("email") or row.get("Email") or row.get("email_address") or "")
                    .strip()
                    .lower()
                    or None
                ),
            )
            for row in rows
            if isinstance(row, dict)
        ]
        return self._records


class CheckMeetingHistoryTool:
    """Check whether there is prior calendar history with the sender."""

    def __init__(self, *, backend: MeetingHistoryBackend | None) -> None:
        self.backend = backend

    def check(self, request: CheckMeetingHistoryInput) -> CheckMeetingHistoryOutput:
        if self.backend is None:
            return CheckMeetingHistoryOutput(
                had_meeting_before=False,
                meeting_count=0,
                last_meeting_at=None,
                matched_as="none",
                confidence=0.0,
                error="calendar_unavailable",
            )

        meetings = self.backend.search_meetings(email=request.email, name=request.name)
        if not meetings:
            return CheckMeetingHistoryOutput(
                had_meeting_before=False,
                meeting_count=0,
                last_meeting_at=None,
                matched_as="none",
                confidence=0.8,
            )

        matched_as: Literal["email", "name", "none"] = (
            "email" if any(item.matched_as == "email" for item in meetings) else "name"
        )
        return CheckMeetingHistoryOutput(
            had_meeting_before=True,
            meeting_count=len(meetings),
            last_meeting_at=_latest_timestamp(item.happened_at for item in meetings),
            matched_as=matched_as,
            confidence=0.98 if matched_as == "email" else 0.88,
        )


class DetectDirectAddressTool:
    """Check whether the fresh message directly addresses Lutz by name."""

    def detect(self, request: DetectDirectAddressInput) -> DetectDirectAddressOutput:
        fresh_body = _fresh_text(request.body)
        lines = [line.strip() for line in fresh_body.splitlines() if line.strip()][:6]
        patterns = [
            r"^(hi|hello|dear|hey)\s+{name}\b[,:!-]?",
            r"^{name}\b[,:!-]",
        ]
        for line in lines:
            lowered_line = line.lower()
            for my_name in request.my_names:
                escaped_name = re.escape(my_name.lower())
                for template in patterns:
                    if re.search(template.format(name=escaped_name), lowered_line):
                        confidence = (
                            0.96
                            if lowered_line.startswith(("hi", "hello", "dear", "hey"))
                            else 0.88
                        )
                        return DetectDirectAddressOutput(
                            directly_addresses_me=True,
                            matched_name=my_name,
                            evidence_snippet=line[:120],
                            confidence=round(confidence, 2),
                        )
        return DetectDirectAddressOutput(
            directly_addresses_me=False,
            matched_name=None,
            evidence_snippet=None,
            confidence=0.08,
        )


class SummarizeRelationshipSignalsTool:
    """Aggregate relationship evidence into a deterministic summary."""

    def summarize(
        self,
        request: SummarizeRelationshipSignalsInput,
    ) -> SummarizeRelationshipSignalsOutput:
        signals: list[RelationshipSignal] = []
        if request.contacts_match.in_contacts:
            signals.append(
                RelationshipSignal(
                    signal="in_contacts",
                    value=True,
                    confidence=request.contacts_match.confidence,
                    strength="strong",
                    reason_code="sender_in_contacts",
                )
            )
        if request.meeting_history.had_meeting_before:
            signals.append(
                RelationshipSignal(
                    signal="had_meeting_before",
                    value=True,
                    confidence=request.meeting_history.confidence,
                    strength="strong",
                    reason_code="prior_meeting",
                )
            )
        if request.written_before.has_written_before:
            signals.append(
                RelationshipSignal(
                    signal="has_written_before",
                    value=True,
                    confidence=request.written_before.confidence,
                    strength="strong",
                    reason_code="prior_written_before",
                )
            )
        if request.non_automated_reply.has_non_automated_reply:
            signals.append(
                RelationshipSignal(
                    signal="has_non_automated_reply",
                    value=True,
                    confidence=request.non_automated_reply.confidence,
                    strength="strong",
                    reason_code="prior_non_automated_reply",
                )
            )
        if request.direct_address.directly_addresses_me:
            signals.append(
                RelationshipSignal(
                    signal="directly_addresses_me",
                    value=True,
                    confidence=request.direct_address.confidence,
                    strength="strong",
                    reason_code="direct_address",
                )
            )
        if request.linkedin_match.found:
            signals.append(
                RelationshipSignal(
                    signal="linkedin_match",
                    value=True,
                    confidence=request.linkedin_match.confidence,
                    strength="weak",
                    reason_code="linkedin_match",
                )
            )
        if request.known_person_mention.mentions_known_person:
            signals.append(
                RelationshipSignal(
                    signal="mentions_known_person",
                    value=True,
                    confidence=request.known_person_mention.confidence,
                    strength="weak",
                    reason_code="known_person_mention",
                )
            )

        strong_count = sum(1 for signal in signals if signal.strength == "strong")
        weak_count = sum(1 for signal in signals if signal.strength == "weak")
        relationship_score = round(min(0.55 * strong_count + 0.3 * weak_count, 0.99), 2)
        explanation = (
            "No relationship evidence found."
            if not signals
            else "Sender is known via " + ", ".join(signal.signal for signal in signals) + "."
        )
        return SummarizeRelationshipSignalsOutput(
            relationship_signals=signals,
            relationship_score=relationship_score,
            has_relationship_evidence=bool(signals),
            strong_signal_count=strong_count,
            weak_signal_count=weak_count,
            explanation=explanation,
        )


class OtherToPersonalDecisionEngine:
    """Deterministically decide whether Other should be upgraded to Personal."""

    def decide(self, request: RelationshipDecisionInput) -> RelationshipDecisionOutput:
        existing = request.existing_category.strip().lower()
        if existing not in {"other", "l1/other"}:
            return RelationshipDecisionOutput(
                final_category="unchanged",
                override_applied=False,
                reason_codes=[],
                human_explanation="The email already belongs to a more specific category.",
                confidence=0.98,
            )

        summary = request.relationship_summary
        reason_codes = [signal.reason_code for signal in summary.relationship_signals]
        newsletter_like = _looks_like_newsletter(request.email)
        no_reply_sender = _looks_like_noreply_sender(request.email.sender_email)
        strong_codes = {
            signal.reason_code
            for signal in summary.relationship_signals
            if signal.strength == "strong"
        }
        weak_codes = {
            signal.reason_code
            for signal in summary.relationship_signals
            if signal.strength == "weak"
        }

        if newsletter_like and strong_codes == set() and weak_codes == {"known_person_mention"}:
            return RelationshipDecisionOutput(
                final_category="L1/Other",
                override_applied=False,
                reason_codes=reason_codes,
                human_explanation=(
                    "Kept L1/Other because the message looks like a newsletter and only weak "
                    "person-mention evidence was found."
                ),
                confidence=0.9,
            )

        if no_reply_sender and strong_codes.issubset({"direct_address"}) and len(strong_codes) <= 1:
            return RelationshipDecisionOutput(
                final_category="L1/Other",
                override_applied=False,
                reason_codes=reason_codes,
                human_explanation=(
                    "Kept L1/Other because the sender looks automated and there is no stronger "
                    "relationship evidence beyond direct address."
                ),
                confidence=0.88,
            )

        if (
            _looks_like_generic_shared_inbox(request.email.sender_email)
            and strong_codes.issubset({"direct_address"})
            and weak_codes.issubset(
                {"prior_inbound_history", "linkedin_match", "known_person_mention"}
            )
        ):
            return RelationshipDecisionOutput(
                final_category="L1/Other",
                override_applied=False,
                reason_codes=reason_codes,
                human_explanation=(
                    "Kept L1/Other because a generic company inbox with only direct address "
                    "and weak non-reciprocal signals is not enough relationship evidence."
                ),
                confidence=0.86,
            )

        if summary.strong_signal_count >= 1:
            return RelationshipDecisionOutput(
                final_category="L1/Personal",
                override_applied=True,
                reason_codes=reason_codes,
                human_explanation=(
                    "Moved to L1/Personal because direct relationship evidence exists: "
                    + ", ".join(reason_codes)
                    + "."
                ),
                confidence=round(min(0.78 + 0.05 * summary.strong_signal_count, 0.98), 2),
            )

        if summary.weak_signal_count >= 2:
            return RelationshipDecisionOutput(
                final_category="L1/Personal",
                override_applied=True,
                reason_codes=reason_codes,
                human_explanation=(
                    "Moved to L1/Personal because multiple weaker relationship signals agree: "
                    + ", ".join(reason_codes)
                    + "."
                ),
                confidence=round(min(0.7 + 0.04 * summary.weak_signal_count, 0.9), 2),
            )

        if summary.weak_signal_count == 1:
            return RelationshipDecisionOutput(
                final_category="L1/Other",
                override_applied=False,
                reason_codes=reason_codes,
                human_explanation=(
                    "Kept L1/Other because only one weak relationship signal was found."
                ),
                confidence=0.8,
            )

        return RelationshipDecisionOutput(
            final_category="L1/Other",
            override_applied=False,
            reason_codes=[],
            human_explanation="Kept L1/Other because no relationship evidence was found.",
            confidence=0.92,
        )


class OtherToPersonalWorkflow:
    """Run the full deterministic Other->Personal relationship workflow."""

    def __init__(
        self,
        *,
        linkedin_tool: LinkedInCsvLookupTool,
        sent_history_tool: SearchSentEmailHistoryTool,
        non_automated_reply_tool: SearchNonAutomatedRepliesTool,
        mention_tool: DetectKnownPersonMentionTool,
        contacts_tool: CheckContactsTool,
        meeting_tool: CheckMeetingHistoryTool,
        direct_address_tool: DetectDirectAddressTool,
        summary_tool: SummarizeRelationshipSignalsTool,
        decision_engine: OtherToPersonalDecisionEngine,
        my_names: list[str] | None = None,
    ) -> None:
        self.linkedin_tool = linkedin_tool
        self.sent_history_tool = sent_history_tool
        self.non_automated_reply_tool = non_automated_reply_tool
        self.mention_tool = mention_tool
        self.contacts_tool = contacts_tool
        self.meeting_tool = meeting_tool
        self.direct_address_tool = direct_address_tool
        self.summary_tool = summary_tool
        self.decision_engine = decision_engine
        self.my_names = my_names or ["Lutz", "Lutz Finger"]

    def evaluate(
        self,
        *,
        existing_category: str,
        email: RelationshipRoutingEmail,
    ) -> RelationshipRoutingWorkflowOutput:
        linkedin_match = self.linkedin_tool.lookup(
            LookupLinkedinCsvInput(name=email.sender_name or "")
        )
        written_before = self.sent_history_tool.search(
            SearchSentEmailHistoryInput(email=email.sender_email)
        )
        non_automated_reply = self.non_automated_reply_tool.search(
            SearchNonAutomatedRepliesInput(email=email.sender_email)
        )
        known_person_mention = self.mention_tool.detect(
            DetectKnownPersonMentionInput(subject=email.subject, body=email.body)
        )
        contacts_match = self.contacts_tool.check(
            CheckContactsInput(email=email.sender_email, name=email.sender_name)
        )
        meeting_history = self.meeting_tool.check(
            CheckMeetingHistoryInput(email=email.sender_email, name=email.sender_name)
        )
        direct_address = self.direct_address_tool.detect(
            DetectDirectAddressInput(
                subject=email.subject,
                body=email.body,
                my_names=self.my_names,
            )
        )
        relationship_summary = self.summary_tool.summarize(
            SummarizeRelationshipSignalsInput(
                linkedin_match=linkedin_match,
                written_before=written_before,
                non_automated_reply=non_automated_reply,
                known_person_mention=known_person_mention,
                contacts_match=contacts_match,
                meeting_history=meeting_history,
                direct_address=direct_address,
            )
        )
        decision = self.decision_engine.decide(
            RelationshipDecisionInput(
                existing_category=existing_category,
                relationship_summary=relationship_summary,
                email=email,
            )
        )
        return RelationshipRoutingWorkflowOutput(
            email=email,
            existing_category=existing_category,
            linkedin_match=linkedin_match,
            written_before=written_before,
            non_automated_reply=non_automated_reply,
            known_person_mention=known_person_mention,
            contacts_match=contacts_match,
            meeting_history=meeting_history,
            direct_address=direct_address,
            relationship_summary=relationship_summary,
            decision=decision,
        )


def tool_io_json_schemas() -> dict[str, dict[str, Any]]:
    """Return JSON schemas for all workflow tool input/output payloads."""

    pairs = {
        "lookup_linkedin_csv": (LookupLinkedinCsvInput, LookupLinkedinCsvOutput),
        "search_sent_email_history": (
            SearchSentEmailHistoryInput,
            SearchSentEmailHistoryOutput,
        ),
        "search_non_automated_replies": (
            SearchNonAutomatedRepliesInput,
            SearchNonAutomatedRepliesOutput,
        ),
        "detect_known_person_mention": (
            DetectKnownPersonMentionInput,
            DetectKnownPersonMentionOutput,
        ),
        "check_contacts": (CheckContactsInput, CheckContactsOutput),
        "check_meeting_history": (
            CheckMeetingHistoryInput,
            CheckMeetingHistoryOutput,
        ),
        "detect_direct_address": (
            DetectDirectAddressInput,
            DetectDirectAddressOutput,
        ),
        "summarize_relationship_signals": (
            SummarizeRelationshipSignalsInput,
            SummarizeRelationshipSignalsOutput,
        ),
        "decision_engine": (RelationshipDecisionInput, RelationshipDecisionOutput),
    }
    return {
        tool_name: {
            "input": cast(Any, input_model).model_json_schema(),
            "output": cast(Any, output_model).model_json_schema(),
        }
        for tool_name, (input_model, output_model) in pairs.items()
    }


def _exact_name_match(*, name: str, candidates: list[str]) -> str | None:
    normalized_target = _normalized_name(name)
    return next(
        (candidate for candidate in candidates if _normalized_name(candidate) == normalized_target),
        None,
    )


def _best_fuzzy_name_match(
    *,
    name: str,
    candidates: list[str],
) -> tuple[str, float, bool] | None:
    normalized_target = _normalized_name(name)
    if not normalized_target:
        return None
    scored = sorted(
        (
            (
                candidate,
                SequenceMatcher(None, normalized_target, _normalized_name(candidate)).ratio(),
            )
            for candidate in candidates
            if _normalized_name(candidate)
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scored:
        return None
    best_name, best_ratio = scored[0]
    ambiguous = len(scored) > 1 and abs(best_ratio - scored[1][1]) <= 0.02
    return best_name, best_ratio, ambiguous


def _contains_name(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name.lower())}\b", text.lower()) is not None


def _normalized_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())).strip()


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _extract_candidate_names(text: str) -> list[str]:
    pattern = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}\b")
    return sorted({match.group(0).strip() for match in pattern.finditer(text)})


def _fresh_text(text: str) -> str:
    truncated = text
    split_markers = (
        "\nOn ",
        "\nFrom:",
        "\n> ",
        "\n-----Original Message-----",
    )
    for marker in split_markers:
        index = truncated.find(marker)
        if index != -1:
            truncated = truncated[:index]
    lines = []
    for line in truncated.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _latest_timestamp(values: Any) -> str | None:
    candidates = [value for value in values if isinstance(value, datetime)]
    if not candidates:
        return None
    return max(candidates).astimezone(UTC).isoformat()


def _looks_like_non_automated_reply(message: SentMessageRecord) -> bool:
    if message.is_automated:
        return False
    if not _looks_like_human_text(message):
        return False
    subject = message.subject.lower()
    return message.is_reply or subject.startswith("re:")


def _looks_like_human_text(message: SentMessageRecord) -> bool:
    subject = message.subject.lower()
    body = _fresh_text(message.body).lower()
    automated_markers = (
        "auto-reply",
        "out of office",
        "calendar response",
        "accepted:",
        "declined:",
        "tentative:",
        "appointment booked",
        "appointment canceled",
        "do not reply",
        "noreply",
        "no-reply",
    )
    if any(marker in subject or marker in body for marker in automated_markers):
        return False
    return len(body.strip()) >= 12 or len(subject.strip()) >= 8


def _looks_like_newsletter(email: RelationshipRoutingEmail) -> bool:
    text = f"{email.subject}\n{email.body}".lower()
    newsletter_markers = (
        "unsubscribe",
        "manage preferences",
        "view online",
        "newsletter",
        "digest",
    )
    return any(marker in text for marker in newsletter_markers)


def _looks_like_noreply_sender(sender_email: str) -> bool:
    lowered = sender_email.lower()
    markers = ("noreply", "no-reply", "donotreply", "do-not-reply")
    return any(marker in lowered for marker in markers)


def _looks_like_generic_shared_inbox(sender_email: str) -> bool:
    lowered = sender_email.lower()
    local_part = lowered.split("@", 1)[0] if "@" in lowered else lowered
    return local_part in {"contact", "hello", "info", "team", "office", "founders", "partners"}
