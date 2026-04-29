from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.control_plane.schedule_gate import mark_slot_completed, select_due_slot


def test_select_due_slot_returns_first_due_slot_for_today(tmp_path: Path) -> None:
    state_path = tmp_path / "gate.json"

    decision = select_due_slot(
        state_path=state_path,
        slots=["17:00", "08:00"],
        now=datetime.fromisoformat("2026-04-02T08:05:00-07:00"),
    )

    assert decision.due is True
    assert decision.slot == "08:00"
    assert decision.slot_key == "2026-04-02@08:00"


def test_select_due_slot_skips_completed_slot_and_picks_later_one(tmp_path: Path) -> None:
    state_path = tmp_path / "gate.json"
    mark_slot_completed(
        state_path=state_path,
        slot_key="2026-04-02@08:00",
        now=datetime.fromisoformat("2026-04-02T08:06:00-07:00"),
    )

    decision = select_due_slot(
        state_path=state_path,
        slots=["08:00", "17:00"],
        now=datetime.fromisoformat("2026-04-02T17:05:00-07:00"),
    )

    assert decision.due is True
    assert decision.slot == "17:00"
    assert decision.slot_key == "2026-04-02@17:00"


def test_select_due_slot_returns_not_due_before_first_slot(tmp_path: Path) -> None:
    decision = select_due_slot(
        state_path=tmp_path / "gate.json",
        slots=["07:00"],
        now=datetime.fromisoformat("2026-04-02T06:59:00-07:00"),
    )

    assert decision.due is False
    assert decision.slot is None
    assert decision.slot_key is None


def test_single_daily_slot_is_not_due_again_after_mark(tmp_path: Path) -> None:
    state_path = tmp_path / "gate.json"
    mark_slot_completed(
        state_path=state_path,
        slot_key="2026-04-02@00:00",
        now=datetime.fromisoformat("2026-04-02T08:00:00-07:00"),
    )

    decision = select_due_slot(
        state_path=state_path,
        slots=["00:00"],
        now=datetime.fromisoformat("2026-04-02T18:30:00-07:00"),
    )

    assert decision.due is False
    assert decision.slot is None
    assert decision.slot_key is None
