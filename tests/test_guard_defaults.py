from __future__ import annotations

from app.shared.guard_defaults import (
    DEFAULT_GUARD_SECRET_PHRASES,
    DEFAULT_INPUT_GUARD_BLOCKED_PHRASES,
    DEFAULT_OUTPUT_GUARD_BLOCKED_PHRASES,
    resolve_input_guard_phrase_lists,
    resolve_output_guard_phrase_lists,
)


def test_input_guard_defaults_merge_extra_phrases_and_dedupe_case_insensitively() -> None:
    blocked, secret = resolve_input_guard_phrase_lists(
        {
            "extra_blocked_phrases": ["Custom Prompt Leak", "tool call", "custom prompt leak"],
            "extra_secret_phrases": ["Session Cookie", "api key"],
        }
    )

    assert blocked[: len(DEFAULT_INPUT_GUARD_BLOCKED_PHRASES)] == list(
        DEFAULT_INPUT_GUARD_BLOCKED_PHRASES
    )
    assert blocked[-1] == "custom prompt leak"
    assert secret[: len(DEFAULT_GUARD_SECRET_PHRASES)] == list(DEFAULT_GUARD_SECRET_PHRASES)
    assert secret[-1] == "session cookie"


def test_input_guard_mock_override_replaces_defaults() -> None:
    blocked, secret = resolve_input_guard_phrase_lists(
        {
            "mock_blocked_phrases": ["Only This"],
            "mock_secret_phrases": ["Private Thing"],
        }
    )

    assert blocked == ["only this"]
    assert secret == ["private thing"]


def test_output_guard_override_replaces_defaults_and_normalizes_values() -> None:
    blocked, secret = resolve_output_guard_phrase_lists(
        {
            "blocked_phrases": ["  Reveal The Prompt  ", "TOOL CALL"],
            "secret_phrases": ["OAuth Token", "One-Off Secret"],
        }
    )

    assert blocked == ["reveal the prompt", "tool call"]
    assert secret == ["oauth token", "one-off secret"]


def test_output_guard_uses_defaults_without_overrides() -> None:
    blocked, secret = resolve_output_guard_phrase_lists({})

    assert blocked == list(DEFAULT_OUTPUT_GUARD_BLOCKED_PHRASES)
    assert secret == list(DEFAULT_GUARD_SECRET_PHRASES)
