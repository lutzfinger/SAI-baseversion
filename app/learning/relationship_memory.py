"""Persist reusable relationship evidence from deeper contact investigations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.shared.config import Settings
from app.tools.personal_relationship_routing import (
    RelationshipSignal,
    SummarizeRelationshipSignalsOutput,
)
from app.workers.contact_investigation_models import ContactInvestigationItem


class RelationshipMemoryEntry(BaseModel):
    """Latest reusable relationship evidence for one sender email."""

    model_config = ConfigDict(extra="forbid")

    sender_email: str
    sender_name: str | None = None
    relationship_summary: SummarizeRelationshipSignalsOutput
    evidence_sources: list[str] = Field(default_factory=list)
    known_relationship: str = "unknown"
    source_workflow_id: str
    source_message_id: str
    updated_at: datetime


class RelationshipMemoryStore:
    """Tiny local JSON-backed store for relationship evidence lookups."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, RelationshipMemoryEntry] | None = None
        self._cache_mtime_ns: int | None = None

    def lookup(self, sender_email: str) -> RelationshipMemoryEntry | None:
        key = sender_email.strip().lower()
        if not key:
            return None
        return self._load().get(key)

    def upsert_many(self, entries: list[RelationshipMemoryEntry]) -> int:
        if not entries:
            return 0
        payload = self._load()
        updated = 0
        for entry in entries:
            key = entry.sender_email.strip().lower()
            if not key:
                continue
            payload[key] = entry
            updated += 1
        if updated == 0:
            return 0
        self._write(payload)
        return updated

    def _load(self) -> dict[str, RelationshipMemoryEntry]:
        if not self.path.exists():
            self._cache = {}
            self._cache_mtime_ns = None
            return {}
        stat = self.path.stat()
        if self._cache is not None and self._cache_mtime_ns == stat.st_mtime_ns:
            return dict(self._cache)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            self._cache = {}
            self._cache_mtime_ns = stat.st_mtime_ns
            return {}
        parsed = {
            str(email).strip().lower(): RelationshipMemoryEntry.model_validate(entry)
            for email, entry in raw.items()
            if str(email).strip()
        }
        self._cache = parsed
        self._cache_mtime_ns = stat.st_mtime_ns
        return dict(parsed)

    def _write(self, payload: dict[str, RelationshipMemoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            key: entry.model_dump(mode="json")
            for key, entry in sorted(payload.items())
        }
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(serialized, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
        self._cache = dict(payload)
        self._cache_mtime_ns = self.path.stat().st_mtime_ns


def record_contact_relationship_memory(
    *,
    settings: Settings,
    workflow_id: str,
    items: list[ContactInvestigationItem],
) -> int:
    """Store relationship evidence from contact-investigation for future triage."""

    entries = [
        entry
        for item in items
        if (entry := _entry_from_contact_investigation_item(item=item, workflow_id=workflow_id))
        is not None
    ]
    store = RelationshipMemoryStore(settings.relationship_memory_path)
    return store.upsert_many(entries)


def _entry_from_contact_investigation_item(
    *,
    item: ContactInvestigationItem,
    workflow_id: str,
) -> RelationshipMemoryEntry | None:
    signals: list[RelationshipSignal] = []
    assessment = item.assessment
    gmail_history = item.gmail_history
    calendar_history = item.calendar_history
    linkedin = item.linkedin

    if assessment.known_contact:
        signals.append(
            RelationshipSignal(
                signal="known_contact",
                value=True,
                confidence=assessment.confidence,
                strength="strong",
                reason_code="known_contact",
            )
        )
    elif assessment.known_relationship != "unknown":
        signals.append(
            RelationshipSignal(
                signal="known_relationship",
                value=True,
                confidence=assessment.confidence,
                strength="strong",
                reason_code=assessment.known_relationship,
            )
        )

    prior_outbound = _safe_int(gmail_history.get("prior_outbound_count"))
    prior_total = _safe_int(gmail_history.get("prior_total_count"))
    if prior_outbound > 0 or prior_total > 0:
        signals.append(
            RelationshipSignal(
                signal="prior_email_history",
                value=True,
                confidence=0.9 if prior_outbound > 0 else 0.84,
                strength="strong",
                reason_code="prior_email_history",
            )
        )

    prior_meeting_count = _safe_int(calendar_history.get("prior_meeting_count"))
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

    if bool(linkedin.get("matched", False)):
        signals.append(
            RelationshipSignal(
                signal="linkedin_match",
                value=True,
                confidence=0.72,
                strength="weak",
                reason_code="linkedin_match",
            )
        )

    if not signals:
        return None

    strong_count = sum(1 for signal in signals if signal.strength == "strong")
    weak_count = sum(1 for signal in signals if signal.strength == "weak")
    summary = SummarizeRelationshipSignalsOutput(
        relationship_signals=signals,
        relationship_score=round(min(0.55 * strong_count + 0.3 * weak_count, 0.99), 2),
        has_relationship_evidence=True,
        strong_signal_count=strong_count,
        weak_signal_count=weak_count,
        explanation="Persisted relationship evidence from contact investigation.",
    )
    return RelationshipMemoryEntry(
        sender_email=item.message.from_email.strip().lower(),
        sender_name=item.message.from_name,
        relationship_summary=summary,
        evidence_sources=list(dict.fromkeys(assessment.evidence_sources)),
        known_relationship=assessment.known_relationship,
        source_workflow_id=workflow_id,
        source_message_id=item.message.message_id,
        updated_at=datetime.now(UTC),
    )


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
