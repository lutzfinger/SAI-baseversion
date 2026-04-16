from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.workers.email_models import EmailMessage


class ConnectorAction(BaseModel):
    """One policy-gated action a connector needs before it can read data."""

    action: str
    reason: str


class ConnectorDescriptor(BaseModel):
    """Metadata the control plane records about the connector being used."""

    component_name: str
    source_details: dict[str, Any] = Field(default_factory=dict)


class EmailConnector(Protocol):
    """Minimal protocol shared by the local fixture and Gmail connectors."""

    def required_actions(self) -> list[ConnectorAction]:
        """Return connector-specific policy checks required before reading."""

    def describe(self) -> ConnectorDescriptor:
        """Return safe metadata that can be written to the audit trail."""

    def fetch_messages(self) -> list[EmailMessage]:
        """Return email messages for a workflow run."""
