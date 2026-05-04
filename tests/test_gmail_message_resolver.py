"""Tests for the Gmail message resolver.

The Gmail API call is stubbed so tests are free + offline. Only
``build_query`` and the result-shaping logic are exercised here.
End-to-end tests against a real Gmail account live in the integration
suite (gated by env var).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.eval.gmail_message_resolver import (
    ResolveCandidate,
    ResolveResult,
    build_query,
    resolve,
    resolve_with_fallback,
)
from app.workers.email_models import EmailMessage


# ─── build_query ───────────────────────────────────────────────────────


class TestBuildQuery:
    def test_email_address_uses_from(self) -> None:
        assert build_query("acme@example.com") == "from:acme@example.com newer_than:30d"

    def test_email_with_explicit_kind(self) -> None:
        assert (
            build_query("acme@example.com", target_kind="sender_email")
            == "from:acme@example.com newer_than:30d"
        )

    def test_domain_form_strips_leading_at(self) -> None:
        assert build_query("@example.com") == "from:example.com newer_than:30d"
        assert build_query("example.com") == "from:example.com newer_than:30d"

    def test_fuzzy_single_word(self) -> None:
        # Single word with no dot — fuzzy match against display name.
        assert build_query("dinika") == "from:dinika newer_than:30d"

    def test_fuzzy_multi_word_quoted(self) -> None:
        # Multi-word — quoted so Gmail treats it as a phrase.
        assert (
            build_query("Dinika Mahtani")
            == 'from:"Dinika Mahtani" newer_than:30d'
        )

    def test_explicit_fuzzy_kind(self) -> None:
        assert (
            build_query("dinika", target_kind="fuzzy")
            == "from:dinika newer_than:30d"
        )

    def test_no_date_filter_when_days_back_zero(self) -> None:
        assert build_query("acme@example.com", days_back=0) == "from:acme@example.com"

    def test_empty_target_raises(self) -> None:
        with pytest.raises(ValueError, match="empty target_pattern"):
            build_query("")
        with pytest.raises(ValueError, match="empty target_pattern"):
            build_query("   ")


# ─── resolve (with stubbed Gmail connector) ────────────────────────────


def _fake_message(
    *, message_id: str, from_email: str, from_name: str | None,
    subject: str, snippet: str, received_at: datetime | None = None,
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=message_id,
        from_email=from_email,
        from_name=from_name,
        to=["operator@example.com"],
        cc=[],
        subject=subject,
        snippet=snippet,
        body_excerpt=snippet,
        received_at=received_at or datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_resolve_returns_zero_matches_cleanly(mock_connector_cls):
    mock_connector_cls.return_value.fetch_messages.return_value = []
    result = resolve(
        "doesnotexist", authenticator=MagicMock(),
    )
    assert result.count == 0
    assert result.has_matches is False
    assert result.error is None
    assert "from:doesnotexist" in result.query_used


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_resolve_returns_one_match(mock_connector_cls):
    mock_connector_cls.return_value.fetch_messages.return_value = [
        _fake_message(
            message_id="m1", from_email="dinika@example.com",
            from_name="Dinika Test", subject="Re: thoughts",
            snippet="hey, on the fund question…",
        ),
    ]
    result = resolve("dinika", authenticator=MagicMock())
    assert result.count == 1
    assert result.is_unique is True
    c = result.candidates[0]
    assert c.from_email == "dinika@example.com"
    assert "thoughts" in c.subject
    assert "2026-05-01" in (c.received_at_iso or "")


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_resolve_returns_many_matches_in_order(mock_connector_cls):
    mock_connector_cls.return_value.fetch_messages.return_value = [
        _fake_message(message_id="m1", from_email="a@example.com",
                      from_name="A", subject="s1", snippet="..."),
        _fake_message(message_id="m2", from_email="b@example.com",
                      from_name="B", subject="s2", snippet="..."),
    ]
    result = resolve("a-or-b", authenticator=MagicMock())
    assert result.count == 2
    assert result.is_unique is False
    assert result.has_matches is True


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_resolve_handles_fetch_exception(mock_connector_cls):
    mock_connector_cls.return_value.fetch_messages.side_effect = RuntimeError(
        "Gmail timeout"
    )
    result = resolve("dinika", authenticator=MagicMock())
    assert result.count == 0
    assert result.error is not None
    assert "Gmail timeout" in result.error


def test_short_summary_is_compact() -> None:
    c = ResolveCandidate(
        message_id="m1", thread_id="t1",
        from_email="dinika@example.com", from_name="Dinika",
        subject="Re: thoughts on the fund",
        snippet="...",
        received_at_iso="2026-05-01T14:00:00+00:00",
    )
    summary = c.short_summary()
    assert "2026-05-01" in summary
    assert "Dinika" in summary
    assert "thoughts" in summary
    # Doesn't contain raw ISO timestamp clutter
    assert "T14:00:00" not in summary


def test_short_summary_truncates_long_subjects() -> None:
    c = ResolveCandidate(
        message_id="m1", thread_id="t1",
        from_email="x@example.com", from_name=None,
        subject="A" * 200, snippet="...",
        received_at_iso="2026-05-01T00:00:00+00:00",
    )
    summary = c.short_summary()
    assert len(summary) < 130


# ─── new query kinds (subject + free_text) ─────────────────────────────


class TestBuildQueryNewKinds:
    def test_subject_kind_uses_subject_clause(self) -> None:
        q = build_query(
            "Security alert for student", target_kind="subject",
        )
        assert 'subject:"Security alert for student"' in q

    def test_subject_kind_quotes_multiword(self) -> None:
        q = build_query("Hello there", target_kind="subject")
        assert "subject:" in q
        assert '"Hello there"' in q

    def test_free_text_quotes_multiword(self) -> None:
        q = build_query("hello world", target_kind="free_text")
        assert '"hello world"' in q

    def test_free_text_unquoted_single_word(self) -> None:
        q = build_query("hello", target_kind="free_text")
        assert q.startswith("hello")


# ─── resolve_with_fallback ─────────────────────────────────────────────


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_fallback_returns_first_hit_skips_subject_and_free_text(mock_cls):
    """First attempt has matches → no fallback."""
    mock_cls.return_value.fetch_messages.return_value = [
        _fake_message(
            message_id="m1", from_email="x@y.com",
            from_name="X", subject="hello", snippet="..."
        ),
    ]
    r = resolve_with_fallback("x@y.com", authenticator=MagicMock())
    assert r.has_matches
    # Only one .resolve() call — first attempt succeeded.
    assert mock_cls.call_count == 1


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_fallback_tries_subject_when_first_attempt_zero(mock_cls):
    """Operator-style: searches by from: returns 0; subject: returns hit."""

    call_results = [
        [],  # first attempt (from: ...) → 0
        [_fake_message(message_id="m1", from_email="x@y.com", from_name="X", subject="Security alert", snippet="...")],  # subject: → 1
    ]

    def make_inner(*args, **kw):
        inner = MagicMock()
        inner.fetch_messages.return_value = call_results.pop(0)
        return inner

    mock_cls.side_effect = make_inner

    r = resolve_with_fallback(
        "Security alert", authenticator=MagicMock(),
    )
    assert r.has_matches
    assert "subject:" in r.query_used
    assert mock_cls.call_count == 2


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_fallback_tries_free_text_after_subject_zero(mock_cls):
    call_results = [
        [],  # from:
        [],  # subject:
        [_fake_message(message_id="m1", from_email="x@y.com", from_name="X", subject="s", snippet="...")],  # free_text
    ]

    def make_inner(*args, **kw):
        inner = MagicMock()
        inner.fetch_messages.return_value = call_results.pop(0)
        return inner

    mock_cls.side_effect = make_inner

    r = resolve_with_fallback("the term", authenticator=MagicMock())
    assert r.has_matches
    assert mock_cls.call_count == 3


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_fallback_returns_empty_with_attempts_listed_when_all_zero(mock_cls):
    mock_cls.return_value.fetch_messages.return_value = []
    r = resolve_with_fallback("nothing matches", authenticator=MagicMock())
    assert r.count == 0
    assert "tried also" in r.query_used


@patch("app.eval.gmail_message_resolver.GmailAPIConnector")
def test_fallback_skips_subject_step_when_operator_already_specified(mock_cls):
    """If operator already said target_kind=subject, dont re-try subject."""
    call_results = [
        [],  # subject: → 0
        [_fake_message(message_id="m1", from_email="x@y.com", from_name="X", subject="hit", snippet="...")],  # free_text → 1
    ]

    def make_inner(*args, **kw):
        inner = MagicMock()
        inner.fetch_messages.return_value = call_results.pop(0)
        return inner

    mock_cls.side_effect = make_inner

    r = resolve_with_fallback(
        "subject text", target_kind="subject", authenticator=MagicMock(),
    )
    assert r.has_matches
    # 2 calls (subject + free_text), not 3 (we skipped redundant subject retry).
    assert mock_cls.call_count == 2

