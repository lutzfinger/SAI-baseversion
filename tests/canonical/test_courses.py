"""Tests for canonical courses loader (#17 mechanism)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from app.canonical import courses


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    courses.reload()
    yield
    courses.reload()


def _swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "courses.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(courses, "COURSES_PATH", target)
    courses.reload()


def _good_course(course_id: str = "TEST101", **overrides) -> dict:
    base = {
        "course_id": course_id,
        "display_name": "Test Course 101",
        "identifiers": ["TEST101", "test 101"],
        "late_work_policy": "Late work loses 10 percent per day with no exceptions outside documented emergencies.",
        "policy_last_verified": "2026-04-01",
        "current_term": "Spring 2026",
        "term_start": "2026-01-15",
        "term_end": "2026-05-15",
        "from_address": "instructor@example.edu",
    }
    base.update(overrides)
    return base


def test_loads_well_formed_course(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [_good_course()]})
    out = courses.all_courses()
    assert "TEST101" in out
    assert out["TEST101"].display_name == "Test Course 101"


def test_get_course_by_id_returns_none_for_unknown(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [_good_course()]})
    assert courses.get_course_by_id("NOPE") is None


def test_duplicate_course_id_raises(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [
        _good_course("DUP"),
        _good_course("DUP"),
    ]})
    with pytest.raises(ValueError, match="Duplicate"):
        courses.all_courses()


def test_missing_required_field_raises(tmp_path, monkeypatch):
    bad = _good_course()
    del bad["from_address"]
    _swap(tmp_path, monkeypatch, {"courses": [bad]})
    with pytest.raises(Exception):  # Pydantic ValidationError
        courses.all_courses()


def test_from_address_must_have_at(tmp_path, monkeypatch):
    bad = _good_course(from_address="not-an-email")
    _swap(tmp_path, monkeypatch, {"courses": [bad]})
    with pytest.raises(Exception):
        courses.all_courses()


def test_is_active_today_within_range(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [_good_course()]})
    course = courses.get_course_by_id("TEST101")
    assert courses.is_active_today(course, today=date(2026, 3, 1)) is True
    assert courses.is_active_today(course, today=date(2025, 12, 1)) is False
    assert courses.is_active_today(course, today=date(2026, 6, 1)) is False


def test_is_policy_stale_uses_tunable(tmp_path, monkeypatch):
    # Policy verified 200 days before "today" — > default 180 → stale.
    course_yaml = _good_course(policy_last_verified="2025-09-01")
    _swap(tmp_path, monkeypatch, {"courses": [course_yaml]})
    course = courses.get_course_by_id("TEST101")
    assert courses.is_policy_stale(course, today=date(2026, 4, 1)) is True
    # 30 days before: not stale.
    course_yaml2 = _good_course(policy_last_verified="2026-03-01")
    _swap(tmp_path, monkeypatch, {"courses": [course_yaml2]})
    course2 = courses.get_course_by_id("TEST101")
    assert courses.is_policy_stale(course2, today=date(2026, 4, 1)) is False


def test_infer_course_from_text_single_match(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [_good_course()]})
    matches = courses.infer_course_from_text(
        "I need an extension on the TEST101 final.",
        only_active=False,
    )
    assert len(matches) == 1
    assert matches[0].course_id == "TEST101"


def test_infer_course_from_text_no_match(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"courses": [_good_course()]})
    matches = courses.infer_course_from_text(
        "I need an extension on something completely different.",
        only_active=False,
    )
    assert matches == []


def test_infer_course_filters_inactive_when_requested(tmp_path, monkeypatch):
    inactive = _good_course(
        "OLD101",
        identifiers=["OLD101"],
        term_start="2024-01-01",
        term_end="2024-05-01",
    )
    active = _good_course("NOW101", identifiers=["NOW101"])
    _swap(tmp_path, monkeypatch, {"courses": [inactive, active]})
    text = "Asking about OLD101 and NOW101 both."
    matches_active_only = courses.infer_course_from_text(text, only_active=True)
    assert {c.course_id for c in matches_active_only} == {"NOW101"}
    matches_all = courses.infer_course_from_text(text, only_active=False)
    assert {c.course_id for c in matches_all} == {"OLD101", "NOW101"}


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(courses, "COURSES_PATH", tmp_path / "missing.yaml")
    courses.reload()
    assert courses.all_courses() == {}
    assert courses.get_course_by_id("TEST101") is None
