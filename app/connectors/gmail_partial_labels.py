"""Read-only Gmail connector for threads with incomplete taxonomy labels."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_labels import is_taxonomy_classification_label
from app.workers.email_models import EmailMessage


class GmailPartialLabelConnector:
    """Fetch Gmail threads that have only one taxonomy side left: L1 or L2."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        user_id: str = "me",
        max_results: int = 25,
        include_spam_trash: bool = False,
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.user_id = user_id
        self.max_results = max_results
        self.include_spam_trash = include_spam_trash
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Label cleanup requires an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.read_metadata",
                reason=(
                    "Label cleanup needs Gmail thread metadata to detect incomplete "
                    "taxonomy labeling."
                ),
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-partial-labels",
            source_details={
                "user_id": self.user_id,
                "max_results": self.max_results,
                "include_spam_trash": self.include_spam_trash,
                "selection": "threads_with_only_l1_or_only_l2",
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
        labels_response = service.users().labels().list(userId=self.user_id).execute()
        items = labels_response.get("labels", [])
        label_id_to_name = {
            str(item.get("id", "")): str(item.get("name", ""))
            for item in items
            if isinstance(item, dict)
        }
        taxonomy_label_ids = [
            label_id
            for label_id, label_name in label_id_to_name.items()
            if is_taxonomy_classification_label(label_name)
        ]
        if not taxonomy_label_ids:
            return []

        ordered_thread_ids = self._candidate_thread_ids(
            service=service,
            taxonomy_label_ids=taxonomy_label_ids,
        )

        messages: list[EmailMessage] = []
        for thread_id in ordered_thread_ids:
            if len(messages) >= self.max_results:
                break
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
            taxonomy_label_names = _thread_taxonomy_label_names(
                thread_payload=thread_payload,
                label_id_to_name=label_id_to_name,
            )
            if not _has_partial_taxonomy_labels(taxonomy_label_names):
                continue
            messages.append(_email_message_from_thread_payload(thread_payload))
        return messages

    def _candidate_thread_ids(self, *, service: Any, taxonomy_label_ids: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for label_id in taxonomy_label_ids:
            page_token: str | None = None
            while True:
                response = (
                    service.users()
                    .threads()
                    .list(
                        userId=self.user_id,
                        labelIds=[label_id],
                        maxResults=self.max_results,
                        includeSpamTrash=self.include_spam_trash,
                        **({"pageToken": page_token} if page_token else {}),
                    )
                    .execute()
                )
                for item in response.get("threads", []):
                    if not isinstance(item, dict):
                        continue
                    thread_id = str(item.get("id", "")).strip()
                    if not thread_id or thread_id in seen:
                        continue
                    seen.add(thread_id)
                    ordered.append(thread_id)
                if len(ordered) >= self.max_results:
                    return ordered
                next_token = str(response.get("nextPageToken", "")).strip()
                if not next_token:
                    break
                page_token = next_token
        return ordered


def _thread_taxonomy_label_names(
    *,
    thread_payload: dict[str, Any],
    label_id_to_name: dict[str, str],
) -> list[str]:
    names: set[str] = set()
    for message in thread_payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        for label_id in message.get("labelIds", []):
            label_name = label_id_to_name.get(str(label_id), "")
            if label_name and is_taxonomy_classification_label(label_name):
                names.add(label_name)
    return sorted(names)


def _has_partial_taxonomy_labels(label_names: list[str]) -> bool:
    has_l1 = any(name.startswith(("L1/", "SAI/L1/")) for name in label_names)
    has_l2 = any(name.startswith(("L2/", "SAI/L2/")) for name in label_names)
    return has_l1 != has_l2


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
    pairs = getaddresses([raw_value or ""])
    for _, address in pairs:
        if address.strip():
            return address.strip()
    return str(raw_value or "unknown")


def _parse_from_name(raw_value: str | None) -> str | None:
    pairs = getaddresses([raw_value or ""])
    for name, address in pairs:
        if address.strip():
            return name.strip() or None
    return None


def _parse_received_at(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
