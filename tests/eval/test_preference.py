"""Tests for app.eval.preference."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.eval.preference import (
    Preference,
    PreferenceSource,
    PreferenceStrength,
    PreferenceVersion,
)


def _proposed_at() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _proposed_version() -> PreferenceVersion:
    return PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.PROPOSED,
        source=PreferenceSource.COWORK,
        proposed_at=_proposed_at(),
    )


def test_proposed_preference_is_not_active() -> None:
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="Lutz prefers exit row seating",
        current=_proposed_version(),
    )
    assert pref.is_active is False


def test_approved_soft_preference_is_active() -> None:
    approved = PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=_proposed_at(),
        approved_at=_proposed_at() + timedelta(hours=1),
        approved_by="lutz",
    )
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="Lutz prefers exit row seating",
        current=approved,
    )
    assert pref.is_active is True


def test_deprecated_preference_is_inactive_even_if_approved() -> None:
    version = PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=_proposed_at(),
        approved_at=_proposed_at() + timedelta(hours=1),
        deprecated_at=_proposed_at() + timedelta(days=30),
    )
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="Lutz prefers exit row seating",
        current=version,
    )
    assert pref.is_active is False


def test_propose_revision_appends_history_and_marks_old_deprecated() -> None:
    initial = PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=_proposed_at(),
        approved_at=_proposed_at() + timedelta(hours=1),
    )
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="Lutz prefers exit row",
        current=initial,
    )
    refined = PreferenceVersion(
        rule_text="prefer_exit_row UNLESS price_delta > 30",
        strength=PreferenceStrength.PROPOSED,
        source=PreferenceSource.INFERRED,
        proposed_at=_proposed_at() + timedelta(days=14),
    )
    pref.propose_revision(refined)
    assert pref.current is refined
    assert len(pref.history) == 1
    deprecated = pref.history[0]
    assert deprecated.rule_text == "prefer_exit_row"
    assert deprecated.deprecated_at == refined.proposed_at
    assert pref.is_active is False  # current is PROPOSED


def test_serialization_round_trip_preserves_history() -> None:
    initial = PreferenceVersion(
        rule_text="prefer_exit_row",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.COWORK,
        proposed_at=_proposed_at(),
        approved_at=_proposed_at() + timedelta(hours=1),
    )
    refined = PreferenceVersion(
        rule_text="prefer_exit_row UNLESS price_delta > 30",
        strength=PreferenceStrength.SOFT,
        source=PreferenceSource.INFERRED,
        proposed_at=_proposed_at() + timedelta(days=14),
        approved_at=_proposed_at() + timedelta(days=15),
    )
    pref = Preference(
        task_id="travel",
        name="exit_row",
        description="exit row pref",
        current=initial,
    )
    pref.propose_revision(refined)

    raw = pref.model_dump_json()
    restored = Preference.model_validate_json(raw)
    assert restored == pref
    assert len(restored.history) == 1
    assert restored.current.rule_text == refined.rule_text
