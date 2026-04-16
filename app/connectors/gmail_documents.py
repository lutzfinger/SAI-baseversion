"""Read fuller Gmail message bodies and safe attachment text."""

from __future__ import annotations

from email import message_from_bytes, policy
from io import BytesIO
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail import _parse_addresses, _parse_received_at
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_send import (
    _decode_base64url_bytes,
    _extract_original_attachments,
    _extract_original_body_variants,
)
from app.workers.email_models import EmailAttachmentText, EmailDocument, EmailMessage


class GmailDocumentConnector:
    """Fetch a full Gmail message plus bounded attachment text."""

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
                reason="Reading message attachments needs an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.read_metadata",
                reason="Document reads need Gmail headers and thread metadata.",
            ),
            ConnectorAction(
                action="connector.gmail.read_body",
                reason="Document reads need bounded message and attachment text.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-documents",
            source_details={
                "user_id": self.user_id,
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
            },
        )

    def fetch_document(self, *, message_id: str) -> EmailDocument:
        payload, parsed = self._raw_message_payload(message_id=message_id)
        headers = {str(name): str(value) for name, value in parsed.items()}
        from_header = str(headers.get("From", "")).strip()
        from_name = None
        from_email = from_header
        if "<" in from_header and ">" in from_header:
            from_name = from_header.split("<", 1)[0].strip().strip('"') or None
            from_email = from_header.split("<", 1)[1].split(">", 1)[0].strip()

        plain_text, html_text = _extract_original_body_variants(parsed)
        attachment_texts: list[EmailAttachmentText] = []
        if plain_text.strip():
            attachment_texts.append(
                EmailAttachmentText(
                    filename=None,
                    mime_type="text/plain",
                    text=plain_text.strip()[:12000],
                    extraction_method="text_part",
                )
            )
        if html_text.strip():
            attachment_texts.append(
                EmailAttachmentText(
                    filename=None,
                    mime_type="text/html",
                    text=_strip_html_like_noise(html_text)[:12000],
                    extraction_method="html_part",
                )
            )
        for attachment in _extract_original_attachments(parsed):
            filename = str(attachment.get("filename", "")).strip() or None
            mime_type = (
                f"{attachment.get('maintype', 'application')}/"
                f"{attachment.get('subtype', 'octet-stream')}"
            ).lower()
            content = attachment.get("content")
            attachment_texts.append(
                EmailAttachmentText(
                    filename=filename,
                    mime_type=mime_type,
                    text=_best_effort_attachment_text(content, mime_type=mime_type)
                    or f"Attachment present: {filename or mime_type}",
                    extraction_method=(
                        "text_part" if isinstance(content, bytes) else "metadata_only"
                    ),
                )
            )

        message = EmailMessage(
            message_id=message_id,
            thread_id=str(payload.get("threadId", "")).strip() or None,
            from_email=from_email,
            from_name=from_name,
            to=_parse_addresses(headers.get("To")),
            cc=_parse_addresses(headers.get("Cc")),
            delivered_to=_parse_addresses(headers.get("Delivered-To")),
            subject=headers.get("Subject", ""),
            snippet=str(payload.get("snippet", "")).strip(),
            body_excerpt=plain_text[:4000],
            received_at=_parse_received_at(headers.get("Date")),
        )
        return EmailDocument(
            message=message,
            plain_text=plain_text[:20000],
            html_text=html_text[:20000],
            attachment_texts=attachment_texts,
        )

    def _raw_message_payload(self, *, message_id: str) -> tuple[dict[str, Any], Any]:
        service = self._service or self.authenticator.build_service()
        payload = (
            service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="raw")
            .execute()
        )
        raw_value = str(payload.get("raw", "")).strip()
        if not raw_value:
            raise ValueError(f"Gmail raw message payload is missing for {message_id}.")
        raw_bytes = _decode_base64url_bytes(raw_value)
        parsed = message_from_bytes(raw_bytes, policy=policy.default)
        return payload, parsed


def _best_effort_attachment_text(content: object, *, mime_type: str) -> str:
    if not isinstance(content, bytes):
        return ""
    lowered = mime_type.lower()
    if lowered.startswith("text/") or lowered in {
        "application/json",
        "message/rfc822",
        "application/xml",
    }:
        return content.decode("utf-8", errors="replace")[:12000].strip()
    if lowered == "application/pdf":
        return _extract_pdf_text(content)
    return ""


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return ""
    text_parts: list[str] = []
    for page in reader.pages:
        try:
            extracted = page.extract_text() or ""
        except Exception:
            extracted = ""
        if extracted.strip():
            text_parts.append(extracted.strip())
    return "\n\n".join(text_parts)[:12000].strip()


def _strip_html_like_noise(value: str) -> str:
    text = value.replace("\r", " ")
    for token in ("<style", "</style>", "<script", "</script>"):
        text = text.replace(token, f"\n{token}")
    return " ".join(text.split())
