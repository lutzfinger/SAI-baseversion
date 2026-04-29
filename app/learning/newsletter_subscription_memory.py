"""Persist newsletter unsubscribe history and intentional re-subscribe decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.workers.email_models import EmailMessage


class NewsletterSubscriptionEntry(BaseModel):
    """Latest unsubscribe memory for one recurring newsletter resource."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    sender_email: str
    sender_domain: str
    unsubscribe_fingerprints: list[str] = Field(default_factory=list)
    last_unsubscribed_at: datetime
    last_unsubscribe_method: str
    last_message_id: str
    last_subject: str
    unsubscribe_count: int = 1
    intentional_resubscribe_confirmed_at: datetime | None = None
    intentional_resubscribe_confirmed_by: str | None = None
    intentional_resubscribe_question_id: str | None = None
    pending_confirmation_question_id: str | None = None
    pending_confirmation_asked_at: datetime | None = None


@dataclass(frozen=True)
class NewsletterSubscriptionMatch:
    entry: NewsletterSubscriptionEntry
    matched_by: str


class NewsletterSubscriptionMemoryStore:
    """Tiny local JSON-backed store for newsletter unsubscribe state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, NewsletterSubscriptionEntry] | None = None
        self._cache_mtime_ns: int | None = None

    def lookup(self, *, message: EmailMessage) -> NewsletterSubscriptionMatch | None:
        payload = self._load()
        sender_email = _normalize_sender_email(message.from_email)
        if sender_email:
            resource_id = _resource_id_from_sender(sender_email)
            entry = payload.get(resource_id)
            if entry is not None:
                return NewsletterSubscriptionMatch(entry=entry, matched_by="sender_email")

        message_fingerprints = set(_message_unsubscribe_fingerprints(message))
        if not message_fingerprints:
            return None
        for entry in payload.values():
            if message_fingerprints.intersection(entry.unsubscribe_fingerprints):
                return NewsletterSubscriptionMatch(
                    entry=entry,
                    matched_by="unsubscribe_target",
                )
        return None

    def record_unsubscribe(
        self,
        *,
        message: EmailMessage,
        method: str,
    ) -> NewsletterSubscriptionEntry:
        payload = self._load()
        sender_email = _normalize_sender_email(message.from_email)
        if not sender_email:
            raise ValueError("Newsletter unsubscribe memory requires a sender email.")
        resource_id = _resource_id_from_sender(sender_email)
        existing = payload.get(resource_id)
        now = datetime.now(UTC)
        fingerprints = sorted(
            set(existing.unsubscribe_fingerprints if existing else [])
            | set(_message_unsubscribe_fingerprints(message))
        )
        entry = NewsletterSubscriptionEntry(
            resource_id=resource_id,
            sender_email=sender_email,
            sender_domain=_sender_domain(sender_email),
            unsubscribe_fingerprints=fingerprints,
            last_unsubscribed_at=now,
            last_unsubscribe_method=method,
            last_message_id=message.message_id,
            last_subject=(message.subject or "").strip(),
            unsubscribe_count=(existing.unsubscribe_count + 1) if existing else 1,
            intentional_resubscribe_confirmed_at=None,
            intentional_resubscribe_confirmed_by=None,
            intentional_resubscribe_question_id=None,
            pending_confirmation_question_id=None,
            pending_confirmation_asked_at=None,
        )
        payload[resource_id] = entry
        self._write(payload)
        return entry

    def mark_pending_confirmation(
        self,
        *,
        resource_id: str,
        question_id: str,
    ) -> NewsletterSubscriptionEntry:
        payload = self._load()
        entry = payload[resource_id]
        updated = entry.model_copy(
            update={
                "pending_confirmation_question_id": question_id,
                "pending_confirmation_asked_at": datetime.now(UTC),
            }
        )
        payload[resource_id] = updated
        self._write(payload)
        return updated

    def confirm_intent(
        self,
        *,
        resource_id: str,
        question_id: str,
        confirmed_by: str,
    ) -> NewsletterSubscriptionEntry:
        payload = self._load()
        entry = payload[resource_id]
        updated = entry.model_copy(
            update={
                "intentional_resubscribe_confirmed_at": datetime.now(UTC),
                "intentional_resubscribe_confirmed_by": confirmed_by,
                "intentional_resubscribe_question_id": question_id,
                "pending_confirmation_question_id": None,
                "pending_confirmation_asked_at": None,
            }
        )
        payload[resource_id] = updated
        self._write(payload)
        return updated

    def clear_pending_confirmation(
        self,
        *,
        resource_id: str,
    ) -> NewsletterSubscriptionEntry:
        payload = self._load()
        entry = payload[resource_id]
        updated = entry.model_copy(
            update={
                "pending_confirmation_question_id": None,
                "pending_confirmation_asked_at": None,
            }
        )
        payload[resource_id] = updated
        self._write(payload)
        return updated

    def _load(self) -> dict[str, NewsletterSubscriptionEntry]:
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
            str(key).strip(): NewsletterSubscriptionEntry.model_validate(value)
            for key, value in raw.items()
            if str(key).strip()
        }
        self._cache = parsed
        self._cache_mtime_ns = stat.st_mtime_ns
        return dict(parsed)

    def _write(self, payload: dict[str, NewsletterSubscriptionEntry]) -> None:
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


def _resource_id_from_sender(sender_email: str) -> str:
    return f"sender_email:{sender_email.strip().lower()}"


def _normalize_sender_email(value: str) -> str:
    return value.strip().lower()


def _sender_domain(sender_email: str) -> str:
    sender = sender_email.strip().lower()
    if "@" in sender:
        return sender.split("@", 1)[1]
    return sender


def _message_unsubscribe_fingerprints(message: EmailMessage) -> list[str]:
    entries = [*message.list_unsubscribe, *message.unsubscribe_links]
    fingerprints: list[str] = []
    for entry in entries:
        fingerprint = _normalize_unsubscribe_fingerprint(entry)
        if fingerprint and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return fingerprints


def _normalize_unsubscribe_fingerprint(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith("mailto:"):
        parsed = urlparse(raw)
        mailbox = unquote(parsed.path).strip().lower()
        query_keys = ",".join(sorted(key.lower() for key, _ in parse_qsl(parsed.query)))
        return f"mailto:{mailbox}?keys={query_keys}"
    if lowered.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        query_keys = ",".join(sorted(key.lower() for key, _ in parse_qsl(parsed.query)))
        path = parsed.path.rstrip("/") or "/"
        return (
            f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path.lower()}"
            f"?keys={query_keys}"
        )
    return lowered
