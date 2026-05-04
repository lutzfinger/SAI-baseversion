"""Tests for the disambiguation flow.

Covers the decision tree, the number-emoji mapping, and the pending-
choice round-trip. The Gmail resolver is not exercised here — we feed
``ResolveResult`` instances directly so the tests stay fast + offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.eval.disambiguation import (
    MAX_CANDIDATES_SHOWN,
    DisambiguationOutcome,
    candidate_from_pending,
    classify_outcome,
    drop_pending_choice,
    lookup_pending_choice,
    number_emoji_to_index,
    write_pending_choice,
)
from app.eval.gmail_message_resolver import ResolveCandidate, ResolveResult


def _candidate(
    *,
    message_id: str = "m1",
    from_email: str = "alex@example.com",
    from_name: str | None = "Alex",
    subject: str = "Re: thoughts",
    received_at_iso: str | None = "2026-04-30T10:00:00+00:00",
) -> ResolveCandidate:
    return ResolveCandidate(
        message_id=message_id,
        thread_id=message_id,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        snippet="...",
        received_at_iso=received_at_iso,
    )


# ─── classify_outcome ───────────────────────────────────────────────────


class TestClassifyOutcome:
    def test_resolver_error_surfaces_as_none(self) -> None:
        result = ResolveResult(
            query_used="from:alex newer_than:30d",
            candidates=[],
            error="Gmail timeout",
        )
        out = classify_outcome(
            target_pattern="alex", bucket="customers", resolve_result=result,
        )
        assert out.kind == "none"
        assert out.candidates == []
        assert "Couldn't search Gmail" in out.operator_message
        assert "Gmail timeout" in out.operator_message

    def test_zero_matches_no_error_suggests_specific_pattern(self) -> None:
        result = ResolveResult(
            query_used="from:doesnotexist newer_than:30d", candidates=[],
        )
        out = classify_outcome(
            target_pattern="doesnotexist", bucket="customers",
            resolve_result=result,
        )
        assert out.kind == "none"
        assert "Couldn't find any mail matching `doesnotexist`" in out.operator_message
        assert "alex@example.com should be customers" in out.operator_message

    def test_unique_match_shows_summary_and_asks_to_react(self) -> None:
        cand = _candidate(
            from_email="dinika.mahtani@example.com",
            from_name="Dinika Mahtani",
            subject="Re: fund question",
        )
        result = ResolveResult(
            query_used="from:dinika newer_than:30d", candidates=[cand],
        )
        out = classify_outcome(
            target_pattern="dinika", bucket="customers", resolve_result=result,
        )
        assert out.kind == "unique"
        assert len(out.candidates) == 1
        assert out.candidates[0].message_id == "m1"
        msg = out.operator_message
        assert "Found one match for `dinika`" in msg
        assert "Dinika Mahtani" in msg
        assert "fund question" in msg
        assert "L1/customers" in msg
        assert "✅" in msg and "❌" in msg

    def test_multiple_matches_lists_with_numbers(self) -> None:
        # Each candidate gets a distinct from_name so the operator can
        # tell them apart in the list. short_summary uses from_name when
        # present, otherwise from_email.
        candidates = [
            _candidate(
                message_id="m1", from_name="Alex Apple",
                subject="Q3 numbers", from_email="alex@example.com",
            ),
            _candidate(
                message_id="m2", from_name="Alex Banana",
                subject="Yet another email", from_email="alex@another.com",
            ),
            _candidate(
                message_id="m3", from_name=None,
                subject="Anonymous", from_email="alex@third.com",
            ),
        ]
        result = ResolveResult(
            query_used="from:alex newer_than:30d", candidates=candidates,
        )
        out = classify_outcome(
            target_pattern="alex", bucket="customers", resolve_result=result,
        )
        assert out.kind == "multiple"
        assert len(out.candidates) == 3
        msg = out.operator_message
        assert "Found 3 matches" in msg
        assert ":one:" in msg and ":two:" in msg and ":three:" in msg
        # Each row appears so the operator can distinguish them.
        assert "Alex Apple" in msg
        assert "Alex Banana" in msg
        assert "alex@third.com" in msg  # falls back to email when name=None
        assert "L1/customers" in msg

    def test_more_than_max_truncates_with_hint(self) -> None:
        many = [
            _candidate(message_id=f"m{i}", from_email=f"a{i}@example.com")
            for i in range(MAX_CANDIDATES_SHOWN + 3)
        ]
        result = ResolveResult(query_used="from:a newer_than:30d", candidates=many)
        out = classify_outcome(
            target_pattern="a", bucket="customers", resolve_result=result,
        )
        assert out.kind == "multiple"
        # Only the first MAX_CANDIDATES_SHOWN are surfaced.
        assert len(out.candidates) == MAX_CANDIDATES_SHOWN
        msg = out.operator_message
        assert f"and {3} more" in msg
        # The unshown ones shouldn't appear in the Slack message
        assert "a7@example.com" not in msg

    def test_outcome_is_immutable(self) -> None:
        result = ResolveResult(
            query_used="q", candidates=[_candidate()],
        )
        out = classify_outcome(
            target_pattern="alex", bucket="customers", resolve_result=result,
        )
        # Frozen dataclass — assignment should error.
        with pytest.raises(Exception):
            out.kind = "multiple"  # type: ignore[misc]


# ─── number_emoji_to_index ──────────────────────────────────────────────


class TestNumberEmojiToIndex:
    def test_word_form_resolves(self) -> None:
        assert number_emoji_to_index("one") == 0
        assert number_emoji_to_index("five") == 4
        assert number_emoji_to_index("keycap_ten") == 9

    def test_digit_form_resolves(self) -> None:
        assert number_emoji_to_index("1") == 0
        assert number_emoji_to_index("3") == 2
        assert number_emoji_to_index("10") == 9

    def test_case_insensitive(self) -> None:
        assert number_emoji_to_index("ONE") == 0
        assert number_emoji_to_index("Two") == 1

    def test_unrelated_emoji_returns_none(self) -> None:
        for name in ["thumbsup", "rocket", "x", "white_check_mark", ""]:
            assert number_emoji_to_index(name) is None


# ─── pending-choice persistence ─────────────────────────────────────────


class TestPendingChoice:
    def test_round_trip(self, tmp_path: Path) -> None:
        choices_dir = tmp_path / "choices"
        cands = [
            _candidate(message_id="m1", from_email="a@example.com"),
            _candidate(message_id="m2", from_email="b@example.com"),
        ]
        out_path = write_pending_choice(
            choices_dir=choices_dir,
            message_ts="1700000001.123456",
            channel="C123",
            candidates=cands,
            bucket="customers",
            proposed_by="U999",
            source_text="alex should be customers",
        )
        assert out_path.exists()

        payload = lookup_pending_choice(
            choices_dir=choices_dir, message_ts="1700000001.123456",
        )
        assert payload is not None
        assert payload["bucket"] == "customers"
        assert payload["channel"] == "C123"
        assert payload["proposed_by"] == "U999"
        assert payload["source_text"] == "alex should be customers"
        assert len(payload["candidates"]) == 2
        assert payload["candidates"][0]["message_id"] == "m1"
        assert payload["candidates"][1]["from_email"] == "b@example.com"

    def test_lookup_nonexistent_returns_none(self, tmp_path: Path) -> None:
        choices_dir = tmp_path / "choices"
        choices_dir.mkdir()
        assert lookup_pending_choice(
            choices_dir=choices_dir, message_ts="9999.000",
        ) is None

    def test_corrupt_file_is_dropped_and_returns_none(self, tmp_path: Path) -> None:
        choices_dir = tmp_path / "choices"
        choices_dir.mkdir()
        # Write garbage at the expected path.
        path = choices_dir / "choice_1700000002_555.json"
        path.write_text("not valid json {")
        assert lookup_pending_choice(
            choices_dir=choices_dir, message_ts="1700000002.555",
        ) is None
        assert not path.exists()  # also dropped so it stops tripping

    def test_drop_removes_file(self, tmp_path: Path) -> None:
        choices_dir = tmp_path / "choices"
        write_pending_choice(
            choices_dir=choices_dir,
            message_ts="1700000003.111",
            channel="C", candidates=[_candidate()],
            bucket="customers", proposed_by="U", source_text="x",
        )
        drop_pending_choice(
            choices_dir=choices_dir, message_ts="1700000003.111",
        )
        assert lookup_pending_choice(
            choices_dir=choices_dir, message_ts="1700000003.111",
        ) is None

    def test_drop_nonexistent_is_silent(self, tmp_path: Path) -> None:
        choices_dir = tmp_path / "choices"
        choices_dir.mkdir()
        # Should not raise.
        drop_pending_choice(
            choices_dir=choices_dir, message_ts="9999.000",
        )

    def test_empty_message_ts_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            write_pending_choice(
                choices_dir=tmp_path,
                message_ts="",
                channel="C", candidates=[_candidate()],
                bucket="customers", proposed_by="U", source_text="x",
            )


# ─── candidate_from_pending ─────────────────────────────────────────────


class TestCandidateFromPending:
    def _payload(self) -> dict:
        return {
            "candidates": [
                {
                    "message_id": "m1", "thread_id": "t1",
                    "from_email": "a@example.com", "from_name": "A",
                    "subject": "s1", "snippet": "...",
                    "received_at_iso": "2026-04-30T00:00:00+00:00",
                },
                {
                    "message_id": "m2", "thread_id": "t2",
                    "from_email": "b@example.com", "from_name": None,
                    "subject": None, "snippet": None,
                    "received_at_iso": None,
                },
            ]
        }

    def test_in_range_returns_hydrated(self) -> None:
        cand = candidate_from_pending(self._payload(), 0)
        assert cand is not None
        assert cand.message_id == "m1"
        assert cand.from_email == "a@example.com"
        assert cand.from_name == "A"

    def test_handles_null_subject_and_snippet(self) -> None:
        cand = candidate_from_pending(self._payload(), 1)
        assert cand is not None
        assert cand.subject == "(no subject)"
        assert cand.snippet == ""
        assert cand.received_at_iso is None
        # Falls back to message_id when thread_id was set, here t2 wins.
        assert cand.thread_id == "t2"

    def test_out_of_range_returns_none(self) -> None:
        assert candidate_from_pending(self._payload(), 5) is None
        assert candidate_from_pending(self._payload(), -1) is None

    def test_missing_candidates_key_returns_none(self) -> None:
        assert candidate_from_pending({}, 0) is None
