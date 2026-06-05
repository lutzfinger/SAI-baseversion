"""Offline tests for the trip-mileage-log skill — AUTONOMOUS email daemon
(Phases 2/3). No creds, no network, no real sheet. Groups (-k): config,
parse_date, destinations, gates, find_row, distance, reason, resolve_auto,
safety, autonomous, eval_datasets, manifest, send_tool, intake, run_once.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
SKILL_DIR = _BASE / "skills" / "trip-mileage-log"
FIXTURES = _BASE / "tests" / "fixtures" / "trip_mileage"
sys.path.insert(0, str(SKILL_DIR))

import mileage_logic as ml          # noqa: E402
import trip_config                  # noqa: E402
import runner                       # noqa: E402
import safety                       # noqa: E402
import intake                       # noqa: E402
import run_daemon                   # noqa: E402
import send_tool                    # noqa: E402
from app.skills.loader import load_skill_manifest  # noqa: E402


def _ev(loc="", summary="", desc="", start="2026-06-03T10:00"):
    return {"location": loc, "summary": summary, "description": desc, "start": start}


# ─── config (Test P2) ───────────────────────────────────────────────

def test_config_loads_ok_with_distance_block():
    cfg = trip_config.load_trip_config(FIXTURES / "config_ok.yaml")
    assert cfg["home_label"] == "Mountain View"
    assert cfg["distance"]["provider"] == "osrm_nominatim"
    assert cfg["max_local_miles"] == 300
    assert "hello@example.com" in cfg["operator_addresses"]


def test_config_missing_key_fails_closed():
    with pytest.raises(ValueError):
        trip_config.load_trip_config(FIXTURES / "config_missing.yaml")


def test_config_absent_path_fails_closed():
    with pytest.raises(FileNotFoundError):
        trip_config.load_trip_config(FIXTURES / "nope.yaml")


# ─── parse_date / destinations / gates / find_row / distance / reason (pure) ──

@pytest.mark.parametrize("utterance,exp_date,exp_prosp", [
    ("yesterday I went to Berkeley", "2026-06-03", False),
    ("today I drove to SF", "2026-06-04", False),
    ("I am going to Berkeley", "2026-06-04", True),
    ("tomorrow I will go to Oakland", "2026-06-05", True),
    ("on 2026-05-20 I went to Berkeley", "2026-05-20", False),
    ("blah blah no date", None, False),
])
def test_parse_date(utterance, exp_date, exp_prosp):
    assert ml.parse_trip_date(utterance, date(2026, 6, 4)) == (exp_date, exp_prosp)


def test_destinations_address_form():
    assert ml.resolve_destinations([_ev(loc="304 Grimes, Berkeley, CA 94720")], "Mountain View")["places"] == ["Berkeley"]


def test_destinations_lists_all_events_at_each_place():
    res = ml.resolve_destinations([
        _ev(summary="One Medical - Palo Alto", start="2026-06-03T10:00"),
        _ev(loc="Berkeley, CA", summary="Lunch", start="2026-06-03T12:00"),
        _ev(loc="Berkeley, CA", summary="esade lutz", start="2026-06-03T13:00"),
    ], "Mountain View")
    assert res["places"] == ["Palo Alto", "Berkeley"]
    assert res["events_used"] == ["One Medical - Palo Alto", "Lunch", "esade lutz"]


def test_destinations_video_only_uses_utterance():
    res = ml.resolve_destinations([_ev(loc="https://meet.google.com/x")], "Mountain View",
                                  utterance_hint="yesterday I went to Berkeley")
    assert res["places"] == ["Berkeley"] and res["low_confidence"] is True


def test_gates_flight_and_relocation():
    assert ml.is_flight_day(["2026-06-03", "Mountain View", "", "", "", "", "TRUE", "", "", ""],
                            [_ev(loc="Berkeley, CA")])[0] is False           # G is NOT a flight signal
    assert ml.is_flight_day(["2026-06-03", "", "SFO", "JFK", "", "", "", "", "", ""], [])[0] is True
    assert ml.is_flight_day(["2026-06-03"] + [""] * 9, [_ev(summary="Flight UA88")])[0] is True
    assert ml.is_relocation(["2026-06-03", "Mountain View", "", "", "", "Ithaca", "", "", "", ""])[0] is True


def test_find_row_and_conflict():
    assert ml.find_date_row(["Day", "2026-06-03", "2026-06-04"], "2026-06-04") == 3
    assert ml.find_date_row(["Day", "2026-06-03"], "2026-12-31") is None
    assert ml.row_conflict(["2026-06-03"] + [""] * 6 + ["80", "", ""]) == ["H"]


def test_distance_chained_math():
    rt, leg = ml.parse_distance_tab([["Berkeley", "80"], ["Palo Alto", "20"], ["Palo Alto -> Berkeley", "40"]])
    assert ml.chained_loop_miles(["Berkeley"], rt, leg)["miles"] == 80.0
    assert ml.chained_loop_miles(["Palo Alto", "Berkeley"], rt, leg)["miles"] == 90.0


def test_reason_tokens():
    reason = ml.build_reason("2026-06-03", ["Palo Alto", "Berkeley"], ["One Medical", "esade lutz"],
                             "PA 10 + leg 40 + Berkeley 40", "no flight")
    for tok in ["2026-06-03", "Palo Alto", "Berkeley", "esade lutz", "Not a flight", "100%"]:
        assert tok in reason


# ─── resolve_auto (Test P3) — auto-resolve via the connector / fail closed ──

def test_resolve_auto_resolves_and_caches():
    inp = {"utterance": "yesterday I went to Berkeley", "today": "2026-06-04",
           "config": runner._canary_config({}),
           "events": [_ev(loc="Berkeley, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": []}
    plan = runner.build_trip_plan(inp, distance_connector=runner._StubConnector({"Berkeley": 80}))
    assert plan.ok and plan.draft["H"] == 80.0
    assert {"name": "Berkeley", "miles": 80.0} in plan.draft["new_distance_entries"]


def test_resolve_auto_connector_error_fails_closed():
    inp = {"utterance": "yesterday I went to Oakland", "today": "2026-06-04",
           "config": runner._canary_config({}),
           "events": [_ev(loc="Oakland, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": []}
    plan = runner.build_trip_plan(inp, distance_connector=runner._StubConnector({}))  # no Oakland
    assert not plan.ok and plan.blocked_reason == "distance_unresolved"


def test_resolve_auto_no_connector_fails_closed():
    inp = {"utterance": "yesterday I went to Oakland", "today": "2026-06-04",
           "config": runner._canary_config({}),
           "events": [_ev(loc="Oakland, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": []}
    plan = runner.build_trip_plan(inp, distance_connector=None)
    assert not plan.ok and plan.blocked_reason == "distance_unresolved"


# ─── safety gate (Test P4) — fail closed ────────────────────────────

def test_safety_gate_safe_unsafe_and_failclosed():
    draft = {"date_str": "2026-06-03", "H": 80, "I": 100, "places": ["Berkeley"]}
    ok = safety.review_mileage_write(draft, predict=lambda role, req: SimpleNamespace(output={"safe": True}))
    assert ok.safe is True
    bad = safety.review_mileage_write(draft, predict=lambda role, req: SimpleNamespace(output={"safe": False, "reason": "looks like a flight"}))
    assert bad.safe is False and "flight" in bad.reason

    def boom(role, req):
        raise RuntimeError("provider down")
    assert safety.review_mileage_write(draft, predict=boom).safe is False  # fail-closed


# ─── autonomous orchestrator (Test P5) ──────────────────────────────

def _happy_inputs(**over):
    inp = {"utterance": "yesterday I went to Berkeley", "today": "2026-06-04",
           "config": runner._canary_config({}),
           "events": [_ev(loc="Berkeley, CA", start="2026-06-03T13:00")],
           "sheet_col_a": ["Day", "2026-06-03"], "row_values": ["2026-06-03"] + [""] * 9,
           "distance_rows": [], "thread_id": "t"}
    inp.update(over)
    return inp


def _safe(_d):
    return SimpleNamespace(safe=True, reason="")


def test_autonomous_writes_when_safe():
    ws = runner._CaptureWS()
    res = runner.run_autonomous(_happy_inputs(), distance_connector=runner._StubConnector({"Berkeley": 80}),
                                safety_reviewer=_safe, ws_opener=lambda u, g: ws, send_enabled=True)
    assert res.verdict == "wrote" and res.wrote is True
    assert ("H155".replace("155", str(res.draft["row"])), [[80.0]]) in [(a, v) for a, v in ws.updates] or \
        any(a.startswith("H") and v == [[80.0]] for a, v in ws.updates)


def test_autonomous_refuses_when_unsafe():
    ws = runner._CaptureWS()
    res = runner.run_autonomous(_happy_inputs(), distance_connector=runner._StubConnector({"Berkeley": 80}),
                                safety_reviewer=lambda d: SimpleNamespace(safe=False, reason="no"),
                                ws_opener=lambda u, g: ws, send_enabled=True)
    assert res.verdict == "refused" and ws.updates == []


def test_autonomous_kill_switch_off_does_not_write():
    ws = runner._CaptureWS()
    res = runner.run_autonomous(_happy_inputs(), distance_connector=runner._StubConnector({"Berkeley": 80}),
                                safety_reviewer=_safe, ws_opener=lambda u, g: ws, send_enabled=False)
    assert res.wrote is False and ws.updates == []


@pytest.mark.parametrize("over,why", [
    ({"row_values": ["2026-06-03", "", "SFO", "JFK", "", "", "", "", "", ""]}, "flight"),
    ({"row_values": ["2026-06-03", "Mountain View", "", "", "", "Ithaca", "", "", "", ""]}, "relocation"),
    ({"sheet_col_a": ["Day", "2026-06-01"]}, "no_row"),
])
def test_autonomous_blocks_no_gate_no_write(over, why):
    ws = runner._CaptureWS()
    gate_calls = []
    res = runner.run_autonomous(_happy_inputs(**over), distance_connector=runner._StubConnector({"Berkeley": 80}),
                                safety_reviewer=lambda d: gate_calls.append(1) or SimpleNamespace(safe=True),
                                ws_opener=lambda u, g: ws, send_enabled=True)
    assert res.verdict == "blocked" and ws.updates == [] and gate_calls == []  # gate not even reached


# ─── eval datasets via run_autonomous (Test P7) ─────────────────────

def _subset(expected, actual):
    return all(actual.get(k) == v for k, v in expected.items())


@pytest.mark.parametrize("fname,n", [("canaries.jsonl", 3), ("edge_cases.jsonl", 6), ("workflow_regression.jsonl", 3)])
def test_eval_datasets(fname, n):
    cases = [json.loads(l) for l in (SKILL_DIR / fname).read_text().splitlines() if l.strip()]
    assert len(cases) >= n
    for case in cases:
        out = runner.run_canary(case)
        assert _subset(case["expected"], out), f"{fname}:{case['case_id']} expected {case['expected']} got {out}"


# ─── manifest autonomous shape (Test P6) ────────────────────────────

def test_manifest_autonomous_shape():
    manifest, report = load_skill_manifest(SKILL_DIR)
    assert report.ok, report.summary()
    kinds = [t.kind for t in manifest.cascade]
    assert "second_opinion" in kinds and "human" not in kinds
    ext = [o for o in manifest.outputs if o.side_effect == "external_write"]
    assert ext and all(o.pre_approved and not o.requires_approval for o in ext)


# ─── send_tool (Test P12) — kill-switch + writes ────────────────────

def _body(**over):
    d = {"workflow_id": "trip-mileage-log", "row": 155, "H": 88.0, "I": 100, "J": "drove",
         "sheet_url": "x", "time_tracking_gid": 1, "distance_gid": 2,
         "new_distance_entries": [], "overwrote": False}
    d.update(over)
    return {"draft": d}


class _WS:
    def __init__(self):
        self.updates = []
        self.appends = []

    def acell(self, a1):
        return SimpleNamespace(value="")

    def update(self, a1, v, value_input_option=None):
        self.updates.append((a1, v))

    def append_row(self, r, value_input_option=None):
        self.appends.append(r)


def test_send_tool_send_enabled_param():
    ws = _WS()
    assert send_tool.apply_approved_proposal(_body(), ws_opener=lambda u, g: ws, send_enabled=False).wrote_sheet is False
    ws2 = _WS()
    res = send_tool.apply_approved_proposal(_body(), ws_opener=lambda u, g: ws2, send_enabled=True)
    assert res.wrote_sheet is True and ("H155", [[88.0]]) in ws2.updates and ("I155", [[100]]) in ws2.updates


def test_send_tool_refuses_bad_body():
    ws = _WS()
    assert send_tool.apply_approved_proposal(_body(H=0), ws_opener=lambda u, g: ws, send_enabled=True).wrote_sheet is False
    assert ws.updates == []


# ─── intake (Test D2) — operator sender validation ──────────────────

def test_intake_operator_only():
    op = ["hello@example.com"]
    good = intake.parse_trigger_email({"from": "Lutz <hello@example.com>", "subject": "yesterday I went to Berkeley"}, operator_addresses=op)
    assert good is not None and "Berkeley" in good.utterance
    assert intake.parse_trigger_email({"from": "stranger@evil.com", "subject": "yesterday I went to Berkeley"}, operator_addresses=op) is None
    assert intake.parse_trigger_email({"from": "hello@example.com", "subject": "FW: invoice"}, operator_addresses=op) is None


# ─── run_once headless cycle (Test D4) ──────────────────────────────

class _FakeGmail:
    def __init__(self, threads, metas, processed_today=0):
        self._threads = threads
        self._metas = metas
        self._processed_today = processed_today
        self.labels = []

    def count_processed_today(self, label):
        return self._processed_today

    def find_unprocessed_threads(self, label):
        return list(self._threads)

    def get_thread_meta(self, tid):
        return self._metas[tid]

    def add_label(self, tid, label):
        self.labels.append((tid, label))


def _fetch_berkeley(date_str):
    return ([_ev(loc="Berkeley, CA", start=date_str + "T13:00")], ["Day", date_str], [date_str] + [""] * 9, [])


_CFG = {"max_writes_per_day": 5, "operator_addresses": ["hello@example.com"], "home_label": "Mountain View",
        "sheet_url": "x", "time_tracking_gid": 1, "distance_gid": 2, "business_pct_default": 100,
        "kill_switch_env": "SAI_TRIP_MILEAGE_SEND_ENABLED", "place_aliases": {}, "max_local_miles": 300}


def test_run_once_happy_writes_and_replies():
    gmail = _FakeGmail(["t1"], {"t1": {"from": "hello@example.com", "subject": "yesterday I went to Berkeley"}})
    replies, ws = [], runner._CaptureWS()
    res = run_daemon.run_once(gmail=gmail, fetch_day_context=_fetch_berkeley, config=_CFG,
                              reply_sender=lambda m, b: replies.append(b), now=date(2026, 6, 4),
                              distance_connector=runner._StubConnector({"Berkeley": 80}),
                              safety_reviewer=_safe, ws_opener=lambda u, g: ws, send_enabled=True)
    assert [o.status for o in res.outcomes] == ["wrote"]
    assert ws.updates and any("logged your trip" in r for r in replies)
    assert ("t1", run_daemon.PROCESSED_LABEL) in gmail.labels


def test_run_once_non_operator_ignored_no_reply():
    gmail = _FakeGmail(["t1"], {"t1": {"from": "stranger@evil.com", "subject": "yesterday I went to Berkeley"}})
    replies = []
    res = run_daemon.run_once(gmail=gmail, fetch_day_context=_fetch_berkeley, config=_CFG,
                              reply_sender=lambda m, b: replies.append(b), now=date(2026, 6, 4),
                              distance_connector=runner._StubConnector({"Berkeley": 80}), safety_reviewer=_safe,
                              ws_opener=lambda u, g: runner._CaptureWS(), send_enabled=True)
    assert [o.status for o in res.outcomes] == ["ignored"] and replies == []
    assert ("t1", run_daemon.PROCESSED_LABEL) in gmail.labels


def test_run_once_flight_needs_human():
    def fetch_flight(date_str):
        return ([], ["Day", date_str], [date_str, "", "SFO", "JFK", "", "", "", "", "", ""], [])
    gmail = _FakeGmail(["t1"], {"t1": {"from": "hello@example.com", "subject": "yesterday I went to New York"}})
    replies, ws = [], runner._CaptureWS()
    res = run_daemon.run_once(gmail=gmail, fetch_day_context=fetch_flight, config=_CFG,
                              reply_sender=lambda m, b: replies.append(b), now=date(2026, 6, 4),
                              distance_connector=runner._StubConnector({}), safety_reviewer=_safe,
                              ws_opener=lambda u, g: ws, send_enabled=True)
    assert [o.status for o in res.outcomes] == ["needs_human"]
    assert ws.updates == [] and any("needs a human" in r for r in replies)


def test_run_once_per_day_cap():
    gmail = _FakeGmail(["t1"], {"t1": {"from": "hello@example.com", "subject": "yesterday I went to Berkeley"}}, processed_today=5)
    res = run_daemon.run_once(gmail=gmail, fetch_day_context=_fetch_berkeley, config=_CFG,
                              reply_sender=lambda m, b: None, now=date(2026, 6, 4),
                              distance_connector=runner._StubConnector({"Berkeley": 80}), safety_reviewer=_safe,
                              ws_opener=lambda u, g: runner._CaptureWS(), send_enabled=True)
    assert res.skipped_reason and res.outcomes == []
