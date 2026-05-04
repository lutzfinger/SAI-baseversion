"""Tests for TA roster loader (#17 mechanism)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.canonical import teaching_assistants as ta_mod


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    ta_mod.reload()
    yield
    ta_mod.reload()


def _swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    target = tmp_path / "teaching_assistants.yaml"
    target.write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(ta_mod, "TA_ROSTER_PATH", target)
    ta_mod.reload()


def _ta(course_id="TEST101", terms=None, last_verified="2026-04-01", **overrides):
    base = {
        "display_name": "Pat Example",
        "email": "pe123@example.edu",
        "course_id": course_id,
        "active_terms": terms if terms is not None else ["Spring 2026"],
        "last_verified": last_verified,
    }
    base.update(overrides)
    return base


def test_loads_roster(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [_ta()]})
    assert len(ta_mod.all_tas()) == 1


def test_email_must_have_at(tmp_path, monkeypatch):
    bad = _ta(email="not-an-email")
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [bad]})
    with pytest.raises(Exception):
        ta_mod.all_tas()


def test_get_active_tas_for_course_filters_by_term(tmp_path, monkeypatch):
    spring = _ta(terms=["Spring 2026"], display_name="Active TA")
    fall = _ta(terms=["Fall 2025"], display_name="Old TA")
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [spring, fall]})
    out = ta_mod.get_active_tas_for_course("TEST101", "Spring 2026")
    assert len(out) == 1
    assert out[0].display_name == "Active TA"


def test_get_active_tas_returns_empty_for_unknown_course(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [_ta()]})
    assert ta_mod.get_active_tas_for_course("OTHER", "Spring 2026") == []


def test_is_roster_stale_when_no_tas_for_course(tmp_path, monkeypatch):
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [_ta(course_id="OTHER")]})
    # No TAs for "TEST101" → stale.
    assert ta_mod.is_roster_stale_for_course("TEST101") is True


def test_is_roster_stale_when_all_outdated(tmp_path, monkeypatch):
    old = _ta(last_verified="2025-09-01")
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [old]})
    # 200+ days before "today" param → stale.
    assert ta_mod.is_roster_stale_for_course(
        "TEST101", today=date(2026, 4, 1),
    ) is True


def test_is_roster_fresh_when_any_recent(tmp_path, monkeypatch):
    old = _ta(last_verified="2025-09-01", display_name="Old")
    fresh = _ta(last_verified="2026-03-01", display_name="Fresh")
    _swap(tmp_path, monkeypatch, {"teaching_assistants": [old, fresh]})
    assert ta_mod.is_roster_stale_for_course(
        "TEST101", today=date(2026, 4, 1),
    ) is False


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ta_mod, "TA_ROSTER_PATH", tmp_path / "missing.yaml")
    ta_mod.reload()
    assert ta_mod.all_tas() == []
    assert ta_mod.is_roster_stale_for_course("TEST101") is True
