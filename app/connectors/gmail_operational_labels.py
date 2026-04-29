"""Read-only Gmail connector for operational labels like SAI/Supervising."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.workers.email_models import EmailMessage


class GmailOperationalLabelConnector:
    """Fetch Gmail threads currently carrying one operational label."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        label_name: str,
        user_id: str = "me",
        max_results: int = 250,
        include_spam_trash: bool = False,
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.label_name = label_name
        self.user_id = user_id
        self.max_results = max_results
        self.include_spam_trash = include_spam_trash
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Supervision review requires an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.read_metadata",
                reason=(
                    "Supervision review reads Gmail metadata for threads labeled "
                    "SAI/Supervising."
                ),
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-operational-labels",
            source_details={
                "user_id": self.user_id,
                "label_name": self.label_name,
                "max_results": self.max_results,
                "include_spam_trash": self.include_spam_trash,
                "mode": "read_only",
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def fetch_messages(self) -> list[EmailMessage]:
        service = self._service or self.authenticator.build_service()
        label_id = _resolve_label_id(
            service=service,
            user_id=self.user_id,
            label_name=self.label_name,
        )
        if label_id is None:
            return []

        messages: list[EmailMessage] = []
        page_token: str | None = None
        while len(messages) < self.max_results:
            response = (
                service.users()
                .threads()
                .list(
                    userId=self.user_id,
                    labelIds=[label_id],
                    maxResults=min(100, self.max_results - len(messages)),
                    includeSpamTrash=self.include_spam_trash,
                    **({"pageToken": page_token} if page_token else {}),
                )
                .execute()
            )
            for item in response.get("threads", []):
                if not isinstance(item, dict):
                    continue
                thread_id = str(item.get("id", "")).strip()
                if not thread_id:
                    continue
                thread_payload = (
                    service.users()
                    .threads()
                    .get(
                        userId=self.user_id,
                        id=thread_id,
                        format="metadata",
                        metadataHeaders=["From", "To", "Cc", "Delivered-To", "Subject", "Date"],
                    )
                    .execute()
                )
                messages.append(_email_message_from_thread_payload(thread_payload))
                if len(messages) >= self.max_results:
                    break
            next_token = str(response.get("nextPageToken", "")).strip()
            if not next_token:
                break
            page_token = next_token
        return messages


def _resolve_label_id(*, service: Any, user_id: str, label_name: str) -> str | None:
    response = service.users().labels().list(userId=user_id).execute()
    for item in response.get("labels", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != label_name:
            continue
        label_id = str(item.get("id", "")).strip()
        if label_id:
            return label_id
    return None


def _email_message_from_thread_payload(thread_payload: dict[str, Any]) -> EmailMessage:
    first_message = next(
        (
            item
            for item in thread_payload.get("messages", [])
            if isinstance(item, dict)
        ),
        {},
    )
    payload = first_message.get("payload", {})
    headers = _header_map(payload)
    return EmailMessage(
        message_id=str(first_message.get("id", thread_payload.get("id", ""))),
        thread_id=str(thread_payload.get("id", "")) or None,
        from_email=_parse_from_email(headers.get("From")),
        from_name=_parse_from_name(headers.get("From")),
        to=_parse_addresses(headers.get("To")),
        cc=_parse_addresses(headers.get("Cc")),
        delivered_to=_parse_addresses(headers.get("Delivered-To")),
        subject=str(headers.get("Subject", "")),
        snippet=str(thread_payload.get("snippet", first_message.get("snippet", ""))),
        body_excerpt="",
        received_at=_parse_received_at(headers.get("Date")),
    )


def _header_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        return {}
    result: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return result


def _parse_addresses(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    values: list[str] = []
    for _, address in getaddresses([raw_value]):
        stripped = address.strip()
        if stripped:
            values.append(stripped)
    return values


def _parse_from_email(raw_value: str | None) -> str:
    if raw_value is None:
        return ""
    addresses = getaddresses([raw_value])
    if not addresses:
        return ""
    return addresses[0][1].strip()


def _parse_from_name(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    addresses = getaddresses([raw_value])
    if not addresses:
        return None
    display_name = addresses[0][0].strip()
    return display_name or None


def _parse_received_at(raw_value: str | None) -> datetime:
    if raw_value is None:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, IndexError):
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
