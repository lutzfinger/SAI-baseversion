"""Shared guard defaults used across all workflows.

These defaults are intentionally central so input/output guard behavior does
not have to be copied into every workflow policy. Policies can still tighten
or extend these values, but they should not need to repeat the common baseline.
"""

from __future__ import annotations

from typing import Any

DEFAULT_INPUT_GUARD_BLOCKED_PHRASES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all instructions",
    "developer message override",
    "roleplay as",
    "pretend to be",
    "jailbreak",
    "ignora las instrucciones anteriores",
    "ignoriere alle vorherigen anweisungen",
    "system prompt",
    "developer message",
    "reveal the prompt",
    "tool call",
    "function call",
    "browse the internet",
)

DEFAULT_OUTPUT_GUARD_BLOCKED_PHRASES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all instructions",
    "system prompt",
    "developer message",
    "reveal the prompt",
    "tool call",
    "function call",
    "browse the internet",
)

DEFAULT_GUARD_SECRET_PHRASES: tuple[str, ...] = (
    "api key",
    "access token",
    "private key",
    "ssh-rsa",
    "oauth token",
)


def resolve_input_guard_phrase_lists(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Resolve input-guard phrase lists from shared defaults plus policy overrides."""

    blocked = _resolve_phrase_list(
        config=config,
        override_keys=("blocked_phrases", "mock_blocked_phrases"),
        extra_key="extra_blocked_phrases",
        default=DEFAULT_INPUT_GUARD_BLOCKED_PHRASES,
    )
    secret = _resolve_phrase_list(
        config=config,
        override_keys=("secret_phrases", "mock_secret_phrases"),
        extra_key="extra_secret_phrases",
        default=DEFAULT_GUARD_SECRET_PHRASES,
    )
    return blocked, secret


def resolve_output_guard_phrase_lists(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Resolve output-guard phrase lists from shared defaults plus policy overrides."""

    blocked = _resolve_phrase_list(
        config=config,
        override_keys=("blocked_phrases",),
        extra_key="extra_blocked_phrases",
        default=DEFAULT_OUTPUT_GUARD_BLOCKED_PHRASES,
    )
    secret = _resolve_phrase_list(
        config=config,
        override_keys=("secret_phrases",),
        extra_key="extra_secret_phrases",
        default=DEFAULT_GUARD_SECRET_PHRASES,
    )
    return blocked, secret


def _resolve_phrase_list(
    *,
    config: dict[str, Any],
    override_keys: tuple[str, ...],
    extra_key: str,
    default: tuple[str, ...],
) -> list[str]:
    for key in override_keys:
        value = config.get(key)
        if isinstance(value, list):
            return _normalize_phrases(value)

    extras = config.get(extra_key)
    if isinstance(extras, list):
        return _dedupe([*default, *extras])

    return list(default)


def _normalize_phrases(values: list[Any]) -> list[str]:
    return _dedupe(str(value) for value in values)


def _dedupe(values: list[Any] | tuple[Any, ...] | Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        lowered = str(value).strip().lower()
        if not lowered or lowered in seen:
            continue
        ordered.append(lowered)
        seen.add(lowered)
    return ordered
