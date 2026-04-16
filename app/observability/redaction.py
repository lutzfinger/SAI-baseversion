from __future__ import annotations

from typing import Any

from pydantic import BaseModel

SENSITIVE_VALUE = "[REDACTED]"
DEFAULT_FULLY_REDACTED_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "refresh_token",
    "token",
}
DEFAULT_TRUNCATED_KEYS = {"body", "body_excerpt", "reasoning", "snippet", "subject"}
DEFAULT_EMAILISH_KEYS = {"bcc", "cc", "email", "from_email", "sender", "to"}


def mask_email(value: str) -> str:
    if "@" not in value:
        return value
    local_part, domain = value.split("@", 1)
    if not local_part:
        return f"***@{domain}"
    return f"{local_part[0]}***@{domain}"


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def redact_payload(
    payload: dict[str, Any],
    max_snippet_chars: int,
    redaction_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return redact_mapping(
        payload,
        max_snippet_chars=max_snippet_chars,
        redaction_config=redaction_config,
    )


def redact_mapping(
    payload: dict[str, Any],
    max_snippet_chars: int,
    redaction_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = redaction_config or {}
    return {
        key: _redact_value(
            key=key,
            value=value,
            max_snippet_chars=int(config.get("snippet_max_chars", max_snippet_chars)),
            redaction_config=config,
        )
        for key, value in payload.items()
    }


def _redact_value(
    key: str,
    value: Any,
    max_snippet_chars: int,
    redaction_config: dict[str, Any],
) -> Any:
    lowered_key = key.lower()
    fully_redacted_keys = {
        item.lower()
        for item in redaction_config.get("full_redaction_keys", DEFAULT_FULLY_REDACTED_KEYS)
    }
    masked_email_keys = {
        item.lower() for item in redaction_config.get("masked_email_keys", DEFAULT_EMAILISH_KEYS)
    }
    truncated_keys = {
        item.lower() for item in redaction_config.get("truncated_keys", DEFAULT_TRUNCATED_KEYS)
    }

    if lowered_key in fully_redacted_keys:
        return SENSITIVE_VALUE
    if isinstance(value, BaseModel):
        return redact_mapping(
            value.model_dump(mode="json"),
            max_snippet_chars=max_snippet_chars,
            redaction_config=redaction_config,
        )
    if isinstance(value, dict):
        return redact_mapping(
            value,
            max_snippet_chars=max_snippet_chars,
            redaction_config=redaction_config,
        )
    if isinstance(value, list):
        return [
            _redact_value(
                key=key,
                value=item,
                max_snippet_chars=max_snippet_chars,
                redaction_config=redaction_config,
            )
            for item in value
        ]
    if isinstance(value, str):
        if lowered_key in masked_email_keys:
            return mask_email(value)
        if lowered_key in truncated_keys:
            return truncate_text(value, limit=max_snippet_chars)
        return value
    return value
