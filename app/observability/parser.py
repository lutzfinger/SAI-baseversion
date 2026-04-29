"""Helpers for inspecting and replaying the append-only audit log.

This module supports the plan's requirement for a simple parser/viewer and
log-driven debugging. It deliberately stays small so operators can trust what
"replay" means: read the stored events back in order.
"""

from __future__ import annotations

from typing import Any

from app.observability.audit import AuditLogger


class AuditLogParser:
    """Read audit events into shapes convenient for the UI and tests."""

    def __init__(self, audit_logger: AuditLogger) -> None:
        self.audit_logger = audit_logger

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return all JSON-serializable events for a single run."""

        events = self.audit_logger.read_events(run_id=run_id)
        return [event.model_dump(mode="json") for event in events]

    def replay_run(self, run_id: str) -> dict[str, Any]:
        """Return a minimal replay bundle for debugging and tests."""

        events = self.events_for_run(run_id)
        return {
            "run_id": run_id,
            "event_count": len(events),
            "timeline": events,
        }
