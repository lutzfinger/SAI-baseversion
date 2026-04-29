"""Local fixture connector used for development and deterministic tests.

This remains the safest default path for test runs: the control plane can still
exercise the full workflow stack without touching a live inbox.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.workers.email_models import EmailMessage


class LocalFileEmailConnector:
    """Load email-like messages from a local JSON fixture file."""

    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path

    def required_actions(self) -> list[ConnectorAction]:
        """Declare the read-only permissions this connector needs."""

        return [
            ConnectorAction(
                action="connector.email.read_metadata",
                reason="Email triage needs metadata to classify messages.",
            ),
            ConnectorAction(
                action="connector.email.read_snippet",
                reason="Email triage needs snippet previews to classify urgency.",
            ),
            ConnectorAction(
                action="connector.email.read_body",
                reason="Email triage can use bounded body excerpts from local fixtures.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        """Return safe metadata about the local source file."""

        return ConnectorDescriptor(
            component_name="connector.local-email",
            source_details={"source_path": str(self.source_path)},
        )

    def fetch_messages(self) -> list[EmailMessage]:
        """Return typed email messages for the first read-only workflow."""

        raw = json.loads(self.source_path.read_text(encoding="utf-8"))
        return [EmailMessage.model_validate(item) for item in raw]
