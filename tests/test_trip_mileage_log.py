"""Offline tests for the trip-mileage-log skill (no creds, no live sheet/calendar).

Groups (run with -k <name>): config, parse_date, destinations, gates, find_row,
distance, reason, eval_datasets, manifest, e2e, send_tool.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

_BASE = Path(__file__).resolve().parent.parent
SKILL_DIR = _BASE / "skills" / "trip-mileage-log"
FIXTURES = _BASE / "tests" / "fixtures" / "trip_mileage"
sys.path.insert(0, str(SKILL_DIR))

import mileage_logic as ml          # noqa: E402
import trip_config                  # noqa: E402
import runner                       # noqa: E402
import send_tool                    # noqa: E402
from app.skills.loader import load_skill_manifest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_handlers():
    # The cascade handler table is global; other test modules clear it.
    runner.register_handlers()
    yield


# ─── config (Test 1) ────────────────────────────────────────────────

def test_config_loads_ok():
    cfg = trip_config.load_trip_config(FIXTURES / "config_ok.yaml")
    assert cfg["home_label"] == "Mountain View"
    assert cfg["place_aliases"]["sutardja"] == "Berkeley"


def test_config_missing_key_fails_closed():
    with pytest.raises(ValueError):
        trip_config.load_trip_config(FIXTURES / "config_missing.yaml")


def test_config_absent_path_fails_closed():
    with pytest.raises(FileNotFoundError):
        trip_config.load_trip_config(FIXTURES / "does_not_exist.yaml")


# ─── parse_date (Test 2) ────────────────────────────────────────────

@pytest.mark.parametrize("utterance,exp_date,exp_prosp", [
    ("yesterday I went to Berkeley", "2026-06-03", False),
    ("today I drove to SF", "2026-06-04", False),
    ("I am going to Berkeley", "2026-06-04", True),
    ("tomorrow I will go to Oakland", "2026-06-05", True),
    ("on 2026-05-20 I went to Berkeley", "2026-05-20", False),
    ("blah blah no date", None, False),
])
def test_parse_date(utterance, exp_date, exp_prosp):
    d, prosp = ml.parse_trip_date(utterance, date(2026, 6, 4))
    assert d == exp_date
    assert prosp == exp_prosp


# ─── destinations (Test 4) ──────────────────────────────────────────

def _ev(loc="", summary="", desc="", start="2026-06-03T10:00"):
    return {"location": loc, "summary": summary, "description": desc, "start": start}


def test_destinations_address_form():
    res = ml.resolve_destinations([_ev(loc="304 Grimes, Berkeley, CA 94720, USA")], "Mountain View")
    assert res["places"] == ["Berkeley"]


def test_destinations_video_only_uses_utterance_hint():
    res = ml.resolve_destinations([_ev(loc="https://meet.google.com/x")], "Mountain View",
                                  utterance_hint="yesterday I went to Berkeley")
    assert res["places"] == ["Berkeley"]
    assert res["low_confidence"] is True


def test_destinations_two_distinct_in_time_order():
    res = ml.resolve_destinations(
        [_ev(loc="Berkeley, CA", start="2026-06-03T12:00"),
         _ev(summary="One Medical - Palo Alto", start="2026-06-03T10:00")], "Mountain View")
    assert res["places"] == ["Palo Alto", "Berkeley"]


def test_destinations_lists_all_events_at_each_place():
    # The reason note must include BOTH One Medical and esade (regression).
    res = ml.resolve_destinations([
        _ev(summary="One Medical - Palo Alto", start="2026-06-03T10:00"),
        _ev(loc="Berkeley, CA", summary="Lunch", start="2026-06-03T12:00"),
        _ev(loc="Berkeley, CA", summary="esade lutz", start="2026-06-03T13:00"),
    ], "Mountain View")
    assert res["places"] == ["Palo Alto", "Berkeley"]
    assert res["events_used"] == ["One Medical - Palo Alto", "Lunch", "esade lutz"]


def test_destinations_two_equal_collapse():
    res = ml.resolve_destinations(
        [_ev(loc="Berkeley, CA", start="2026-06-03T10:00"),
         _ev(loc="UC Berkeley", summary="esade", start="2026-06-03T13:00")],
        "Mountain View", aliases={"uc berkeley": "Berkeley"})
    assert res["places"] == ["Berkeley"]


def test_destinations_too_many():
    res = ml.resolve_destinations(
        [_ev(loc="Berkeley, CA", start="2026-06-03T09:00"),
         _ev(loc="Palo Alto, CA", start="2026-06-03T12:00"),
         _ev(loc="San Jose, CA", start="2026-06-03T15:00")], "Mountain View")
    assert res["too_many"] is True


def test_destinations_home_filtered():
    res = ml.resolve_destinations([_ev(loc="Mountain View, CA")], "Mountain View")
    assert res["places"] == []


# ─── gates (Test 5) ─────────────────────────────────────────────────

def test_flight_gate_clean_drive_day_is_not_flight():
    row = ["2026-06-03", "Mountain View", "", "", "", "", "TRUE", "", "", ""]  # G=TRUE but no airport
    flew, _ = ml.is_flight_day(row, [_ev(loc="Berkeley, CA", summary="esade")])
    assert flew is False  # G is NOT a flight signal


def test_flight_gate_airport_filled():
    row = ["2026-06-03", "", "SFO", "JFK", "", "", "TRUE", "", "", ""]
    assert ml.is_flight_day(row, [])[0] is True


def test_flight_gate_calendar_flight_event():
    assert ml.is_flight_day(["2026-06-03"] + [""] * 9, [_ev(summary="Flight UA88 to JFK")])[0] is True


def test_relocation_gate_b_differs_from_f():
    row = ["2026-06-03", "Mountain View", "", "", "", "Ithaca", "", "", "", ""]
    assert ml.is_relocation(row)[0] is True


def test_relocation_gate_same_or_empty():
    assert ml.is_relocation(["2026-06-03", "Mountain View", "", "", "", "", "", "", "", ""])[0] is False


# ─── find_row + conflict (Test 6) ───────────────────────────────────

def test_find_date_row():
    col_a = ["Day", "2026-06-02", "2026-06-03", "2026-06-04"]
    assert ml.find_date_row(col_a, "2026-06-04") == 4
    assert ml.find_date_row(col_a, "2026-12-31") is None


def test_row_conflict():
    assert ml.row_conflict(["2026-06-03"] + [""] * 6 + ["80", "", ""]) == ["H"]
    assert ml.row_conflict(["2026-06-03"] + [""] * 9) == []


# ─── distance (Test 7) ──────────────────────────────────────────────

def _tab():
    return ml.parse_distance_tab([["Berkeley", "80"], ["Palo Alto", "20"], ["Palo Alto -> Berkeley", "40"]])


def test_distance_single_hit():
    rt, leg = _tab()
    assert ml.chained_loop_miles(["Berkeley"], rt, leg)["miles"] == 80.0


def test_distance_single_miss_asks():
    rt, leg = _tab()
    out = ml.chained_loop_miles(["Oakland"], rt, leg)
    assert out["missing"] == {"round_trip": ["Oakland"]}
    assert "Oakland" in out["ask"]


def test_distance_two_place_chained():
    rt, leg = _tab()
    assert ml.chained_loop_miles(["Palo Alto", "Berkeley"], rt, leg)["miles"] == 90.0  # 10 + 40 + 40


def test_distance_leg_miss_asks():
    rt, leg = ml.parse_distance_tab([["Berkeley", "80"], ["Palo Alto", "20"]])
    out = ml.chained_loop_miles(["Palo Alto", "Berkeley"], rt, leg)
    assert out["missing"]["leg"] == ["Palo Alto -> Berkeley"]


def test_distance_provided_round_trip_satisfies_ask():
    rt, leg = ml.parse_distance_tab([])
    out = ml.chained_loop_miles(["Oakland"], rt, leg, provided={"round_trip": {"Oakland": 50}})
    assert out["miles"] == 50.0


# ─── reason + draft (Test 8) ────────────────────────────────────────

def test_reason_contains_required_tokens():
    reason = ml.build_reason("2026-06-03", ["Palo Alto", "Berkeley"],
                             ["One Medical - Palo Alto", "esade lutz"],
                             "PA one-way 10 + PA->Berkeley 40 + Berkeley one-way 40", "no flight")
    for tok in ["2026-06-03", "Palo Alto", "Berkeley", "esade lutz", "Not a flight", "100%", "Miles"]:
        assert tok in reason


def test_reason_prospective_caveat_and_draft():
    reason = ml.build_reason("2026-06-04", ["Berkeley"], [], "Berkeley round trip = 80 mi",
                             "no flight", prospective=True)
    assert reason.startswith("PROSPECTIVE")
    draft = ml.build_row_draft(155, 80.0, reason, prospective=True)
    assert draft["H"] == 80.0 and draft["I"] == 100 and draft["row"] == 155


# ─── eval datasets via run_canary (Test 9) ──────────────────────────

def _subset(expected: dict, actual: dict) -> bool:
    return all(actual.get(k) == v for k, v in expected.items())


@pytest.mark.parametrize("fname,min_count", [
    ("canaries.jsonl", 3), ("edge_cases.jsonl", 6), ("workflow_regression.jsonl", 3)])
def test_eval_datasets(fname, min_count):
    cases = [json.loads(l) for l in (SKILL_DIR / fname).read_text().splitlines() if l.strip()]
    assert len(cases) >= min_count
    for case in cases:
        outcome = runner.run_canary(case)
        assert _subset(case["expected"], outcome), \
            f"{fname}:{case['case_id']} expected {case['expected']} got {outcome}"


# ─── manifest (Test 10) ─────────────────────────────────────────────

def test_manifest_loads_and_has_required_eval_kinds():
    manifest, report = load_skill_manifest(SKILL_DIR)
    assert report.ok, report.summary()
    kinds = {ds.kind for ds in manifest.eval.datasets}
    assert {"canaries", "edge_cases", "workflow"}.issubset(kinds)
    sheet_outputs = [o for o in manifest.outputs if o.side_effect == "external_write"]
    assert sheet_outputs and all(o.requires_approval for o in sheet_outputs)


# ─── e2e cascade (Test 11) ──────────────────────────────────────────

def _run(inp, tmp_path):
    inp = dict(inp)
    inp.setdefault("thread_id", "t")
    inp.setdefault("config", runner._canary_config(inp))
    return runner.run(inp, extra={"proposed_dir": tmp_path / "proposed"})


def test_e2e_happy_single_stages_proposal(tmp_path):
    inp = {"utterance": "yesterday I went to Berkeley", "today": "2026-06-04",
           "events": [_ev(loc="Berkeley, CA", summary="esade", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"],
           "row_values": ["2026-06-03"] + [""] * 9, "distance_rows": [["Berkeley", "80"]]}
    result = _run(inp, tmp_path)
    assert result.final_verdict == "ready_to_propose"
    body = yaml.safe_load((tmp_path / "proposed" / "t.yaml").read_text())
    assert body["draft"]["H"] == 80.0 and body["draft"]["I"] == 100 and body["draft"]["J"]


def test_e2e_two_place_chained(tmp_path):
    inp = {"utterance": "yesterday I drove to Palo Alto then Berkeley", "today": "2026-06-04",
           "events": [_ev(loc="Palo Alto, CA", start="2026-06-03T10:00"),
                      _ev(loc="Berkeley, CA", start="2026-06-03T12:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": [["Palo Alto", "20"], ["Berkeley", "80"], ["Palo Alto -> Berkeley", "40"]]}
    result = _run(inp, tmp_path)
    assert result.final_verdict == "ready_to_propose"
    assert result.accumulated["draft"]["H"] == 90.0


@pytest.mark.parametrize("mut,reason_prefix", [
    ({"row_values": ["2026-06-03", "", "SFO", "JFK", "", "", "", "", "", ""]}, "flight_day"),
    ({"row_values": ["2026-06-03", "Mountain View", "", "", "", "Ithaca", "", "", "", ""]}, "relocation"),
    ({"sheet_col_a": ["Day", "2026-06-01"]}, "date_row_not_found"),
])
def test_e2e_fail_closed_branches(mut, reason_prefix, tmp_path):
    inp = {"utterance": "yesterday I went to Berkeley", "today": "2026-06-04",
           "events": [_ev(loc="Berkeley, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": [["Berkeley", "80"]]}
    inp.update(mut)
    result = _run(inp, tmp_path)
    assert result.final_verdict == "escalate"
    assert result.final_reason.startswith(reason_prefix)


def test_e2e_distance_miss_asks(tmp_path):
    inp = {"utterance": "yesterday I went to Oakland", "today": "2026-06-04",
           "events": [_ev(loc="Oakland, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": []}
    result = _run(inp, tmp_path)
    assert result.final_verdict == "escalate" and result.final_reason == "distance_ask"
    assert result.accumulated["ask_missing"] == {"round_trip": ["Oakland"]}


def test_e2e_overwrite_conflict_then_confirm(tmp_path):
    base = {"utterance": "yesterday I went to Berkeley", "today": "2026-06-04",
            "events": [_ev(loc="Berkeley, CA", start="2026-06-03T13:00")],
            "sheet_col_a": ["Day", "2026-06-03"],
            "row_values": ["2026-06-03", "", "", "", "", "", "", "80", "", ""],
            "distance_rows": [["Berkeley", "80"]]}
    r1 = _run(base, tmp_path)
    assert r1.final_verdict == "escalate" and r1.final_reason.startswith("row_conflict")
    r2 = _run({**base, "confirm_overwrite": True}, tmp_path)
    assert r2.final_verdict == "ready_to_propose" and r2.accumulated["draft"]["overwrote"] is True


def test_e2e_prospective(tmp_path):
    inp = {"utterance": "I am going to Berkeley", "today": "2026-06-04",
           "events": [_ev(loc="Berkeley, CA", start="2026-06-04T13:00")],
           "sheet_col_a": ["Day", "2026-06-04"], "row_values": ["2026-06-04"] + [""] * 9,
           "distance_rows": [["Berkeley", "80"]]}
    result = _run(inp, tmp_path)
    assert result.final_verdict == "ready_to_propose"
    assert result.accumulated["draft"]["J"].startswith("PROSPECTIVE")


# ─── send_tool (Test 12) ────────────────────────────────────────────

class _FakeWS:
    def __init__(self, existing=None):
        self.updates = []
        self.appends = []
        self._existing = existing or {}

    def acell(self, a1):
        from types import SimpleNamespace
        return SimpleNamespace(value=self._existing.get(a1, ""))

    def update(self, a1, values, value_input_option=None):
        self.updates.append((a1, values))

    def append_row(self, row, value_input_option=None):
        self.appends.append(row)


def _body(**over):
    draft = {"workflow_id": "trip-mileage-log", "row": 155, "H": 89.0, "I": 100,
             "J": "drove MTV -> Palo Alto -> Berkeley -> home", "sheet_url": "x",
             "time_tracking_gid": 1, "distance_gid": 2,
             "new_distance_entries": [{"name": "Berkeley", "miles": 80}],
             "overwrote": False, "prospective": False}
    draft.update(over)
    return {"draft": draft}


def test_send_tool_kill_switch_off_does_not_write(monkeypatch):
    monkeypatch.delenv(send_tool.KILL_SWITCH_ENV, raising=False)
    ws = _FakeWS()
    res = send_tool.apply_approved_proposal(_body(), ws_opener=lambda u, g: ws)
    assert res.wrote_sheet is False and ws.updates == []
    assert "kill_switch_off" in res.reason


def test_send_tool_writes_when_enabled(monkeypatch):
    monkeypatch.setenv(send_tool.KILL_SWITCH_ENV, "1")
    ws = _FakeWS()
    res = send_tool.apply_approved_proposal(_body(), ws_opener=lambda u, g: ws)
    assert res.wrote_sheet is True
    assert ("H155", [[89.0]]) in ws.updates
    assert ("I155", [[100]]) in ws.updates
    assert any(a1 == "J155" for a1, _ in ws.updates)
    assert ws.appends == [["Berkeley", 80]]


def test_send_tool_refuses_bad_bodies(monkeypatch):
    monkeypatch.setenv(send_tool.KILL_SWITCH_ENV, "1")
    ws = _FakeWS()
    assert send_tool.apply_approved_proposal(_body(workflow_id="x"), ws_opener=lambda u, g: ws).wrote_sheet is False
    assert send_tool.apply_approved_proposal(_body(H=0), ws_opener=lambda u, g: ws).wrote_sheet is False
    assert ws.updates == []


def test_send_tool_refuses_unconfirmed_overwrite(monkeypatch):
    monkeypatch.setenv(send_tool.KILL_SWITCH_ENV, "1")
    ws = _FakeWS(existing={"H155": "80"})
    res = send_tool.apply_approved_proposal(_body(overwrote=False), ws_opener=lambda u, g: ws)
    assert res.wrote_sheet is False and "unconfirmed_overwrite" in res.reason
    assert ws.updates == []
