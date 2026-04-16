"""Append-only JSONL audit logging.

This is the primary observability trail from the original plan. The JSONL file
is optimized for local inspection, replay, and incident review, while the
SQLite store handles compact operational state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from app.observability.redaction import redact_payload
from app.shared.config import Settings
from app.shared.models import AuditEvent
from app.shared.run_ids import new_id


class AuditLogger:
    """Write and read append-only audit events."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.audit_log_path
        self._lock = Lock()
        self.settings.ensure_runtime_paths()

    def append_event(
        self,
        *,
        run_id: str,
        workflow_id: str,
        actor: str,
        component: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        redact: bool = True,
        redaction_config: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one redacted event to the audit log.

        Redaction happens at write time so the default path is safe even if a
        caller passes richer input than the logs should retain.
        """

        event_payload = payload or {}
        if redact:
            event_payload = redact_payload(
                event_payload,
                max_snippet_chars=self.settings.max_logged_snippet_chars,
                redaction_config=redaction_config,
            )
        event = AuditEvent(
            event_id=new_id("evt"),
            run_id=run_id,
            workflow_id=workflow_id,
            timestamp=_utc_now(),
            actor=actor,
            component=component,
            event_type=event_type,
            payload=event_payload,
            redacted=redact,
        )
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            json.dump(event.model_dump(mode="json"), handle, sort_keys=True)
            handle.write("\n")
        return event

    def read_events(
        self,
        *,
        run_id: str | None = None,
        workflow_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        """Read audit events back from disk for inspection and replay."""

        if not self.path.exists():
            return []

        results: list[AuditEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                event = AuditEvent.model_validate(raw)
                if run_id is not None and event.run_id != run_id:
                    continue
                if workflow_id is not None and event.workflow_id != workflow_id:
                    continue
                results.append(event)

        if limit is not None:
            return results[-limit:]
        return results


def _utc_now() -> datetime:
    """Small helper so timestamps are consistent across audit writes."""

    return datetime.now(UTC)
