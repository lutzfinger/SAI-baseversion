"""Narrow Gmail send connector for controlled outbound Gmail actions."""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage as MimeEmailMessage
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator


class GmailSendConnectorError(RuntimeError):
    """Raised when an outbound Gmail send violates connector policy."""


class GmailSendConnector:
    """Send tightly scoped Gmail messages, including rich forwarded originals."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        user_id: str = "me",
        service: Any | None = None,
        allowed_recipient_emails: list[str] | tuple[str, ...] | None = None,
        allowed_recipient_domains: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.user_id = user_id
        self._service = service
        self.allowed_recipient_emails = {
            _normalize_email(value)
            for value in (allowed_recipient_emails or [])
            if _normalize_email(value)
        }
        self.allowed_recipient_domains = {
            _normalize_domain(value)
            for value in (allowed_recipient_domains or [])
            if _normalize_domain(value)
        }

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Sending unsubscribe email requires an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.send_message",
                reason=(
                    "Controlled Gmail workflows require sending a narrowly scoped outbound email."
                ),
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-send",
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
        return str(self.authenticator.auth_summary().get("account", "")).strip()

    def _service_client(self) -> Any:
        return self._service or self.authenticator.build_service()

    def _send_mime_message(
        self,
        *,
        mime_message: MimeEmailMessage,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        service = self._service_client()
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        created = service.users().messages().send(userId=self.user_id, body=payload).execute()
        return {
            "message_id": str(created.get("id", "")),
            "thread_id": str(created.get("threadId", thread_id or "")),
            "to_email": str(mime_message.get("To", "")).strip(),
            "subject": str(mime_message.get("Subject", "")).strip(),
            "from_email": self.account_email(),
        }

    def send_plaintext_message(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        from_email: str | None = None,
        reply_to_email: str | None = None,
    ) -> dict[str, Any]:
        self._assert_allowed_recipient(to_email)
        mime_message = MimeEmailMessage()
        mime_message["To"] = to_email
        mime_message["Subject"] = subject
        if from_email:
            mime_message["From"] = from_email
        if reply_to_email:
            mime_message["Reply-To"] = reply_to_email.strip()
        mime_message.set_content(body)
        send_result = self._send_mime_message(mime_message=mime_message, thread_id=thread_id)
        if from_email:
            send_result["from_email"] = from_email.strip()
        if reply_to_email:
            send_result["reply_to_email"] = reply_to_email.strip()
        return send_result

    def forward_gmail_message(
        self,
        *,
        to_email: str,
        original_message_id: str,
        original_subject: str,
        original_from_email: str,
        original_from_name: str | None = None,
        original_received_at: datetime | None = None,
        original_to: list[str] | None = None,
        original_cc: list[str] | None = None,
        note: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_allowed_recipient(to_email)
        raw_original = self._fetch_raw_message_bytes(message_id=original_message_id)
        original_message = message_from_bytes(raw_original, policy=policy.default)
        original_plain, original_html = _extract_original_body_variants(original_message)
        original_attachments = _extract_original_attachments(original_message)
        if not original_plain and not original_html:
            raise ValueError(
                f"Original Gmail message {original_message_id} does not contain a readable body."
            )

        resolved_subject = (
            str(original_message.get("Subject", "")).strip() or original_subject.strip()
        )
        forward_subject = (
            resolved_subject
            if resolved_subject.lower().startswith("fwd:")
            else f"Fwd: {resolved_subject}"
        )
        forwarded_headers = _forwarded_header_lines(
            original_message=original_message,
            original_from_email=original_from_email,
            original_from_name=original_from_name,
            original_received_at=original_received_at,
            original_subject=resolved_subject,
            original_to=original_to,
            original_cc=original_cc,
        )
        plain_body = _build_forward_plaintext_body(
            note=note,
            forwarded_headers=forwarded_headers,
            original_plain=original_plain,
            original_html=original_html,
        )
        html_body = _build_forward_html_body(
            note=note,
            forwarded_headers=forwarded_headers,
            original_plain=original_plain,
            original_html=original_html,
        )

        mime_message = MimeEmailMessage()
        mime_message["To"] = to_email
        mime_message["Subject"] = forward_subject
        mime_message.set_content(plain_body)
        if html_body:
            mime_message.add_alternative(html_body, subtype="html")
        for attachment in original_attachments:
            mime_message.add_attachment(
                attachment["content"],
                maintype=str(attachment["maintype"]),
                subtype=str(attachment["subtype"]),
                filename=str(attachment["filename"]),
            )
        mime_message.add_attachment(
            raw_original,
            maintype="message",
            subtype="rfc822",
            filename=_forwarded_message_filename(resolved_subject),
        )
        send_result = self._send_mime_message(
            mime_message=mime_message,
            thread_id=thread_id,
        )
        send_result.update(
            {
                "forward_mode": "rich_gmail_forward",
                "original_message_id": original_message_id,
                "attached_original_filename": _forwarded_message_filename(resolved_subject),
                "original_attachment_count": len(original_attachments),
                "forwarded_attachment_count": len(original_attachments),
                "forwarded_attachment_filenames": [
                    str(attachment["filename"]) for attachment in original_attachments
                ],
            }
        )
        return send_result

    def _fetch_raw_message_bytes(self, *, message_id: str) -> bytes:
        service = self._service_client()
        payload = (
            service.users()
            .messages()
            .get(
                userId=self.user_id,
                id=message_id,
                format="raw",
            )
            .execute()
        )
        raw_value = str(payload.get("raw", "")).strip()
        if not raw_value:
            raise ValueError(f"Gmail raw message payload is missing for {message_id}.")
        return _decode_base64url_bytes(raw_value)

    def forward_message_copy(
        self,
        *,
        to_email: str,
        original_from_email: str,
        original_subject: str,
        original_body: str,
        original_from_name: str | None = None,
        original_received_at: datetime | None = None,
        original_to: list[str] | None = None,
        original_cc: list[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._assert_allowed_recipient(to_email)
        forward_subject = (
            original_subject
            if original_subject.lower().startswith("fwd:")
            else f"Fwd: {original_subject}"
        )
        body_lines: list[str] = []
        if note:
            body_lines.extend([note.strip(), ""])
        body_lines.append("---------- Forwarded message ----------")
        from_display = original_from_email.strip()
        if original_from_name:
            from_display = f"{original_from_name.strip()} <{from_display}>"
        body_lines.append(f"From: {from_display}")
        if original_received_at is not None:
            body_lines.append(f"Date: {original_received_at.isoformat()}")
        body_lines.append(f"Subject: {original_subject.strip()}")
        if original_to:
            normalized_to = [value.strip() for value in original_to if value.strip()]
            if normalized_to:
                body_lines.append(f"To: {', '.join(normalized_to)}")
        if original_cc:
            normalized_cc = [value.strip() for value in original_cc if value.strip()]
            if normalized_cc:
                body_lines.append(f"Cc: {', '.join(normalized_cc)}")
        body_lines.extend(["", original_body.strip()])
        return self.send_plaintext_message(
            to_email=to_email,
            subject=forward_subject,
            body="\n".join(body_lines).strip(),
        )

    def _assert_allowed_recipient(self, recipient_email: str) -> None:
        normalized_email = _normalize_email(recipient_email)
        if not normalized_email:
            raise GmailSendConnectorError("Outbound Gmail recipient email is empty.")
        if not self.allowed_recipient_emails and not self.allowed_recipient_domains:
            return
        if normalized_email in self.allowed_recipient_emails:
            return
        domain = normalized_email.split("@", 1)[1] if "@" in normalized_email else ""
        if domain and domain in self.allowed_recipient_domains:
            return
        raise GmailSendConnectorError(
            f"Outbound Gmail recipient is not allowed by connector policy: {normalized_email}"
        )


def _decode_base64url_bytes(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("utf-8"))


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_domain(value: str) -> str:
    return value.strip().lower().lstrip("@")


def _extract_original_body_variants(original_message: Any) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in original_message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_filename():
            continue
        content_type = part.get_content_type().lower()
        try:
            payload = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = str(payload or "").strip()
        if not text:
            continue
        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(text)
    plain_text = "\n\n".join(part for part in plain_parts if part).strip()
    html_text = "\n\n".join(part for part in html_parts if part).strip()
    return plain_text, html_text


def _extract_original_attachments(original_message: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    attachment_index = 1
    for part in original_message.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get_content_disposition() or "").strip().lower()
        filename = str(part.get_filename() or "").strip()
        if disposition != "attachment" and not filename:
            continue
        content = part.get_payload(decode=True)
        if content is None:
            try:
                payload = part.get_content()
            except Exception as exc:
                raise ValueError(
                    "Could not preserve original invoice attachments from Gmail."
                ) from exc
            if isinstance(payload, bytes):
                content = payload
            else:
                charset = part.get_content_charset() or "utf-8"
                content = str(payload).encode(charset, errors="replace")
        maintype, subtype = _content_type_parts(part.get_content_type())
        attachments.append(
            {
                "filename": filename or f"attachment-{attachment_index}",
                "maintype": maintype,
                "subtype": subtype,
                "content": content,
            }
        )
        attachment_index += 1
    return attachments


def _content_type_parts(content_type: str) -> tuple[str, str]:
    if "/" not in content_type:
        return "application", "octet-stream"
    maintype, subtype = content_type.split("/", 1)
    return maintype.strip().lower() or "application", subtype.strip().lower() or "octet-stream"


def _forwarded_header_lines(
    *,
    original_message: Any,
    original_from_email: str,
    original_from_name: str | None,
    original_received_at: datetime | None,
    original_subject: str,
    original_to: list[str] | None,
    original_cc: list[str] | None,
) -> list[tuple[str, str]]:
    from_display = str(original_message.get("From", "")).strip()
    if not from_display:
        from_display = original_from_email.strip()
        if original_from_name:
            from_display = f"{original_from_name.strip()} <{from_display}>"
    date_display = str(original_message.get("Date", "")).strip()
    if not date_display and original_received_at is not None:
        date_display = original_received_at.isoformat()
    subject_display = str(original_message.get("Subject", "")).strip() or original_subject.strip()
    to_display = str(original_message.get("To", "")).strip()
    if not to_display and original_to:
        to_display = ", ".join(value.strip() for value in original_to if value.strip())
    cc_display = str(original_message.get("Cc", "")).strip()
    if not cc_display and original_cc:
        cc_display = ", ".join(value.strip() for value in original_cc if value.strip())

    headers: list[tuple[str, str]] = [("From", from_display)]
    if date_display:
        headers.append(("Date", date_display))
    headers.append(("Subject", subject_display))
    if to_display:
        headers.append(("To", to_display))
    if cc_display:
        headers.append(("Cc", cc_display))
    return headers


def _build_forward_plaintext_body(
    *,
    note: str | None,
    forwarded_headers: list[tuple[str, str]],
    original_plain: str,
    original_html: str,
) -> str:
    body_lines: list[str] = []
    if note:
        body_lines.extend([note.strip(), ""])
    body_lines.append("---------- Forwarded message ----------")
    body_lines.extend(f"{name}: {value}" for name, value in forwarded_headers)
    body_lines.append("")
    if original_plain.strip():
        body_lines.append(original_plain.strip())
    elif original_html.strip():
        body_lines.append(_html_to_text(original_html))
    return "\n".join(line for line in body_lines if line is not None).strip()


def _build_forward_html_body(
    *,
    note: str | None,
    forwarded_headers: list[tuple[str, str]],
    original_plain: str,
    original_html: str,
) -> str:
    header_lines = "".join(
        (f"<div><strong>{html.escape(name)}:</strong> {html.escape(value)}</div>")
        for name, value in forwarded_headers
    )
    rendered_original = ""
    if original_html.strip():
        rendered_original = _forwarded_html_fragment(original_html)
    elif original_plain.strip():
        rendered_original = (
            '<pre style="white-space: pre-wrap; font-family: Arial, sans-serif; '
            'font-size: 14px; line-height: 1.5; margin: 0;">'
            f"{html.escape(original_plain.strip())}</pre>"
        )
    if not rendered_original:
        return ""

    note_block = ""
    if note:
        note_block = (
            '<p style="font-family: Arial, sans-serif; font-size: 14px; '
            'line-height: 1.5; margin: 0 0 16px 0;">'
            f"{html.escape(note.strip())}</p>"
        )
    return (
        "<html><body>"
        f"{note_block}"
        '<div style="border-top: 1px solid #d0d7de; margin-top: 8px; padding-top: 12px;">'
        '<div style="font-family: Arial, sans-serif; font-size: 13px; '
        'line-height: 1.5; color: #444; margin-bottom: 16px;">'
        '<div style="font-weight: 600; margin-bottom: 8px;">'
        "---------- Forwarded message ----------"
        "</div>"
        f"{header_lines}"
        "</div>"
        f"{rendered_original}"
        "</div>"
        "</body></html>"
    )


def _forwarded_html_fragment(original_html: str) -> str:
    without_scripts = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "",
        original_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    styles = re.findall(
        r"<style\b[^>]*>.*?</style>",
        without_scripts,
        flags=re.IGNORECASE | re.DOTALL,
    )
    body_match = re.search(
        r"<body\b[^>]*>(.*)</body>",
        without_scripts,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = body_match.group(1).strip() if body_match else without_scripts.strip()
    style_block = "\n".join(styles).strip()
    if style_block:
        return f"{style_block}\n{fragment}"
    return fragment


def _html_to_text(raw_html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", raw_html)
    normalized = html.unescape(without_tags)
    return re.sub(r"\s+", " ", normalized).strip()


def _forwarded_message_filename(subject: str) -> str:
    normalized = re.sub(r"[^\w.\-]+", "_", subject.strip()).strip("._")
    safe_stem = normalized or "original_message"
    return f"{safe_stem}.eml"
