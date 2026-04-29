"""Narrow Gmail connector for archiving threads after summary delivery."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator


class GmailArchiveConnector:
    """Archive Gmail threads by removing the INBOX label only."""

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
                reason="Archiving newsletter threads requires an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.archive_thread",
                reason=(
                    "The newsletter summary workflow archives newsletter "
                    "threads after posting summaries."
                ),
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-archive",
            source_details={
                "user_id": self.user_id,
                "archive_mode": "remove_inbox_label_only",
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def archive_thread(self, *, thread_id: str) -> dict[str, str]:
        service = self._service or self.authenticator.build_service()
        (
            service.users()
            .threads()
            .modify(
                userId=self.user_id,
                id=thread_id,
                body={"removeLabelIds": ["INBOX"]},
            )
            .execute()
        )
        return {"thread_id": thread_id, "removed_label": "INBOX"}
