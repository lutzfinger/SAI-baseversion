"""Official Gmail API connector for live read-only triage runs.

The connector is intentionally narrow:
- Gmail API only
- read-only scope only
- metadata, snippet, and a bounded plain-text body excerpt
- no attachment downloads
- no hidden writes or mailbox mutations
"""

from __future__ import annotations

import base64
import html
import re
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.shared.config import get_settings
from app.workers.email_models import EmailMessage


class GmailAPIConnector:
    """Fetch email metadata and snippets through the official Gmail API."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        user_id: str = "me",
        query: str | None = None,
        label_ids: list[str] | None = None,
        max_results: int = 10,
        include_spam_trash: bool = False,
        max_body_chars: int | None = None,
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.user_id = user_id
        self.query = query
        self.label_ids = ["INBOX"] if label_ids is None else list(label_ids)
        self.max_results = max_results
        self.include_spam_trash = include_spam_trash
        self.max_body_chars = max_body_chars or get_settings().max_email_body_chars
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        """Declare the exact Gmail permissions this connector needs."""

        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Live Gmail access requires an explicit OAuth login on this machine.",
            ),
            ConnectorAction(
                action="connector.gmail.read_metadata",
                reason="Email triage needs Gmail metadata to classify messages.",
            ),
            ConnectorAction(
                action="connector.gmail.read_snippet",
                reason="Email triage needs Gmail snippets to classify urgency.",
            ),
            ConnectorAction(
                action="connector.gmail.read_body",
                reason="Meeting detection needs a bounded plain-text Gmail body excerpt.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        """Return safe connector metadata for the audit log and dashboard."""

        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-api",
            source_details={
                "user_id": self.user_id,
                "query": self.query or "",
                "label_ids": list(self.label_ids),
                "max_results": self.max_results,
                "include_spam_trash": self.include_spam_trash,
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def fetch_messages(self) -> list[EmailMessage]:
        """Return read-only Gmail messages as shared `EmailMessage` objects."""

        service = self._service or self.authenticator.build_service()
        list_kwargs: dict[str, Any] = {
            "userId": self.user_id,
            "maxResults": self.max_results,
            "includeSpamTrash": self.include_spam_trash,
        }
        if self.query:
            list_kwargs["q"] = self.query
        if self.label_ids:
            list_kwargs["labelIds"] = self.label_ids
        list_request = service.users().messages().list(**list_kwargs)
        response = cast_to_mapping(list_request.execute())
        message_refs = response.get("messages", [])
        if not isinstance(message_refs, list):
            return []

        messages: list[EmailMessage] = []
        for item in message_refs:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id", "")).strip()
            if not message_id:
                continue
            get_request = (
                service.users()
                .messages()
                .get(
                    userId=self.user_id,
                    id=message_id,
                    format="full",
                )
            )
            raw_message = cast_to_mapping(get_request.execute())
            messages.append(
                _message_from_gmail_payload(
                    raw_message,
                    max_body_chars=self.max_body_chars,
                )
            )
        return messages

    def fetch_thread_messages(self, *, thread_id: str) -> list[EmailMessage]:
        """Return all readable messages for one Gmail thread."""

        service = self._service or self.authenticator.build_service()
        thread_request = (
            service.users()
            .threads()
            .get(
                userId=self.user_id,
                id=thread_id,
                format="full",
            )
        )
        payload = cast_to_mapping(thread_request.execute())
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            return []
        messages: list[EmailMessage] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            messages.append(
                _message_from_gmail_payload(
                    raw_message,
                    max_body_chars=self.max_body_chars,
                )
            )
        return messages


def _message_from_gmail_payload(payload: dict[str, Any], *, max_body_chars: int) -> EmailMessage:
    headers = _header_map(payload.get("payload", {}))
    to_addresses = _parse_addresses(headers.get("To"))
    cc_addresses = _parse_addresses(headers.get("Cc"))
    delivered_to = _unique_preserving_order(
        [
            *_parse_addresses(headers.get("Delivered-To")),
            *_parse_addresses(headers.get("X-Original-To")),
            *_parse_addresses(headers.get("Envelope-To")),
        ]
    )
    from_pairs = getaddresses([headers.get("From", "")])
    from_addresses = [address.strip() for _, address in from_pairs if address.strip()]
    from_name = from_pairs[0][0].strip() if from_pairs and from_pairs[0][0].strip() else None
    from_email = from_addresses[0] if from_addresses else str(headers.get("From", "unknown"))
    body_text, unsubscribe_links = _extract_body_content(
        cast_to_mapping(payload.get("payload", {}))
    )
    body_excerpt = _truncate_text(
        _normalize_whitespace(body_text),
        limit=max_body_chars,
    )
    return EmailMessage(
        message_id=str(payload.get("id", "")),
        thread_id=_optional_string(payload.get("threadId")),
        from_email=from_email,
        from_name=from_name,
        to=to_addresses,
        cc=cc_addresses,
        delivered_to=delivered_to,
        subject=str(headers.get("Subject", "")),
        snippet=str(payload.get("snippet", "")),
        body_excerpt=body_excerpt,
        list_unsubscribe=_parse_list_unsubscribe(headers.get("List-Unsubscribe")),
        list_unsubscribe_post=_optional_string(headers.get("List-Unsubscribe-Post")),
        unsubscribe_links=unsubscribe_links,
        received_at=_parse_received_at(headers.get("Date")),
    )


def _header_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        return {}
    mapping: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        mapping[name] = value
    return mapping


def _parse_addresses(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    values: list[str] = []
    for _, address in getaddresses([raw_value]):
        stripped = address.strip()
        if stripped:
            values.append(stripped)
    return values


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


def cast_to_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_body_content(payload: dict[str, Any]) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    unsubscribe_links: list[str] = []
    _collect_text_parts(
        payload,
        plain_parts=plain_parts,
        html_parts=html_parts,
        unsubscribe_links=unsubscribe_links,
    )
    parts = plain_parts if plain_parts else html_parts
    return (
        "\n\n".join(part for part in parts if part.strip()).strip(),
        _unique_preserving_order(unsubscribe_links),
    )


def _collect_text_parts(
    payload: dict[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
    unsubscribe_links: list[str],
) -> None:
    filename = str(payload.get("filename", "")).strip()
    if filename:
        # The workflow reads message text only. Attachment binaries are never
        # fetched or decoded here.
        return

    mime_type = str(payload.get("mimeType", "")).lower()
    body = cast_to_mapping(payload.get("body", {}))
    data = body.get("data")
    if isinstance(data, str):
        decoded = _decode_base64url(data)
        if mime_type.startswith("text/plain"):
            plain_parts.append(decoded)
        elif mime_type.startswith("text/html"):
            html_parts.append(_html_to_text(decoded))
            unsubscribe_links.extend(_extract_unsubscribe_links_from_html(decoded))

    parts = payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                _collect_text_parts(
                    part,
                    plain_parts=plain_parts,
                    html_parts=html_parts,
                    unsubscribe_links=unsubscribe_links,
                )


def _decode_base64url(data: str) -> str:
    padding = "=" * ((4 - len(data) % 4) % 4)
    decoded = base64.urlsafe_b64decode((data + padding).encode("utf-8"))
    return decoded.decode("utf-8", errors="replace")


def _html_to_text(raw_html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", raw_html)
    return html.unescape(without_tags)


def _extract_unsubscribe_links_from_html(raw_html: str) -> list[str]:
    parser = _UnsubscribeLinkParser()
    parser.feed(raw_html)
    return parser.unsubscribe_links


def _parse_list_unsubscribe(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    matches = re.findall(r"<([^>]+)>", raw_value)
    if matches:
        return _unique_preserving_order([match.strip() for match in matches if match.strip()])
    return _unique_preserving_order([part.strip() for part in raw_value.split(",") if part.strip()])


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return ordered


class _UnsubscribeLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.unsubscribe_links: list[str] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = {name.lower(): value or "" for name, value in attrs}
        href = attributes.get("href", "").strip()
        if href:
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        text = " ".join(part.strip() for part in self._current_text if part.strip()).lower()
        href = self._current_href.strip()
        if "unsubscribe" in href.lower() or "unsubscribe" in text:
            self.unsubscribe_links.append(href)
        self._current_href = None
        self._current_text = []


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate_text(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3].rstrip()}..."
