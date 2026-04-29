"""Minimal Gmail draft-only connector used by approval workflows."""

from __future__ import annotations

import base64
from email.message import EmailMessage as MimeEmailMessage
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator


class GmailDraftConnector:
    """Create Gmail drafts without bundling unrelated mailbox history actions."""

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
                action="connector.gmail.create_draft",
                reason="Reply-planning writes approval drafts only and never sends automatically.",
            )
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-drafts",
            source_details={
                "user_id": self.user_id,
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
                "account": auth_summary.get("account", ""),
            },
        )

    def account_email(self) -> str:
        """Return the authenticated mailbox account when available."""

        return str(self.authenticator.auth_summary().get("account", "")).strip()

    def create_draft(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        from_email: str | None = None,
    ) -> dict[str, Any]:
        service = self._service or self.authenticator.build_service()
        mime_message = MimeEmailMessage()
        mime_message["To"] = to_email
        mime_message["Subject"] = subject
        if from_email:
            mime_message["From"] = from_email
        mime_message.set_content(body)
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        created = (
            service.users()
            .drafts()
            .create(userId=self.user_id, body=payload)
            .execute()
        )
        return {
            "draft_id": str(created.get("id", "")),
            "thread_id": str(created.get("message", {}).get("threadId", thread_id or "")),
            "to_email": to_email,
            "subject": subject,
            "from_email": from_email,
        }
