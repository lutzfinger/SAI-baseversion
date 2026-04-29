"""Runtime-only connector for workflows that do not read mailbox data."""

from __future__ import annotations

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.workers.email_models import EmailMessage


class RuntimeOnlyConnector:
    """No-op connector used by workflow workers that fetch their own data."""

    def required_actions(self) -> list[ConnectorAction]:
        return []

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            component_name="connector.runtime-only",
            source_details={"source": "runtime_only"},
        )

    def fetch_messages(self) -> list[EmailMessage]:
        return []
