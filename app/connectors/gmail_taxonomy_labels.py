"""Read-only Gmail taxonomy-label inspection helpers."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.workers.email_models import all_taxonomy_gmail_label_names


class GmailTaxonomyLabelInspector:
    """Inspect current taxonomy labels on Gmail threads without modifying them."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        user_id: str = "me",
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.user_id = user_id
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Reading Gmail thread labels requires an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.read_metadata",
                reason="Manual label-correction review reads the current Gmail taxonomy labels.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-taxonomy-labels",
            source_details={
                "user_id": self.user_id,
                "label_namespace": "L1/L2",
                "mode": "read_only",
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def list_thread_taxonomy_labels(self, *, thread_id: str) -> list[str]:
        service = self._service or self.authenticator.build_service()
        taxonomy_label_names = set(all_taxonomy_gmail_label_names())
        response = service.users().labels().list(userId=self.user_id).execute()
        items = response.get("labels", [])
        label_id_to_name = {
            str(item.get("id", "")): str(item.get("name", ""))
            for item in items
            if isinstance(item, dict) and str(item.get("name", "")) in taxonomy_label_names
        }
        thread = (
            service.users()
            .threads()
            .get(userId=self.user_id, id=thread_id, format="metadata")
            .execute()
        )
        names: set[str] = set()
        for message in thread.get("messages", []):
            if not isinstance(message, dict):
                continue
            for label_id in message.get("labelIds", []):
                label_name = label_id_to_name.get(str(label_id), "")
                if label_name:
                    names.add(label_name)
        return sorted(names)
