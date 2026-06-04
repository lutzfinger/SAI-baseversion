"""Runner for trip-mileage-log — deterministic, composed SAI workflow.

Cascade (all read-only until the operator ✅; the actual sheet write fires from
send_tool.py per PRINCIPLES §2/§9):

  1. validate_inputs   — need an utterance/date + a loaded config
  2. read_context      — parse date; read the day's calendar + the sheet row +
                         the distance tab (LIVE connectors, or INJECTED data for
                         tests/dry-run, mirroring student-participation-check)
  3. not_flying_gate   — fail closed on a flight day or a B≠F relocation
  4. resolve_distance  — destinations from calendar -> chained-loop miles from
                         the "Distance MTV to" tab; ask (fail closed) on a miss
  5. overwrite_gate    — refuse to clobber an already-filled H/I/J row unless
                         confirm_overwrite
  6. build_draft       — compose H / I=100 / J reason, stage under draft
  7. human             — built-in: stages the approval proposal YAML

run(inputs) -> CascadeResult. Inputs schema:
  utterance:str, today:'YYYY-MM-DD', config_path?:str, thread_id?:str,
  explicit_date?:str, destinations?:[str],
  provided_round_trip?:{place:miles}, provided_leg?:{'A->B':miles},
  confirm_overwrite?:bool,
  # test/dry-run injection (skips live reads):
  events?:[{summary,location,description,start}], sheet_col_a?:[str],
  row_values?:list|dict, distance_rows?:[[name,miles]]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.cascade import (  # noqa: E402
    CascadeContext, CascadeResult, CascadeStep, register_rules_handler, run_cascade,
)
from app.skills.loader import load_skill_manifest  # noqa: E402

# Sibling modules (hyphenated dir -> load by path is unnecessary; same dir is on
# sys.path because Python adds the script's dir, but be explicit for `-m`/import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mileage_logic as ml          # noqa: E402
from trip_config import load_trip_config  # noqa: E402

WORKFLOW_ID = "trip-mileage-log"


# ─── tier handlers ───────────────────────────────────────────────────

def _cfg(ctx: CascadeContext) -> dict[str, Any]:
    cfg = ctx.accumulated.get("config")
    if cfg is None:
        cfg = ctx.inputs.get("config") or load_trip_config(ctx.inputs.get("config_path"))
        ctx.accumulated["config"] = cfg
    return cfg


def validate_inputs_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    if not (ctx.inputs.get("utterance") or ctx.inputs.get("explicit_date")):
        return CascadeStep(kind="escalate", reason="missing_input:need utterance or explicit_date")
    try:
        _cfg(ctx)
    except Exception as exc:  # config absent / invalid -> fail closed (#6)
        return CascadeStep(kind="escalate", reason=f"config_error:{type(exc).__name__}:{exc}")
    return CascadeStep(kind="continue", reason="inputs_ok")


def read_context_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    config = _cfg(ctx)
    today = _parse_today(ctx.inputs.get("today"))
    explicit = ctx.inputs.get("explicit_date")
    if explicit:
        date_str, prospective = explicit, (explicit > today.isoformat())
    else:
        date_str, prospective = ml.parse_trip_date(ctx.inputs.get("utterance", ""), today)
    if not date_str:
        return CascadeStep(kind="escalate", reason="unparseable_date")

    # Calendar events for the day (injected for tests/dry-run, else live).
    if "events" in ctx.inputs:
        events = ctx.inputs["events"]
    else:
        try:
            events = _live_calendar_events(date_str, config)
        except Exception as exc:
            return CascadeStep(kind="escalate", reason=f"calendar_read_failed:{type(exc).__name__}:{exc}")

    # Sheet context (injected, else live).
    if "sheet_col_a" in ctx.inputs or "row_values" in ctx.inputs:
        col_a = ctx.inputs.get("sheet_col_a", [])
        row_values = ctx.inputs.get("row_values")
        distance_rows = ctx.inputs.get("distance_rows", [])
        row_idx = ml.find_date_row(col_a, date_str) if col_a else ctx.inputs.get("row_idx")
        if row_idx is None and row_values is not None:
            row_idx = ctx.inputs.get("row_idx", -1)
    else:
        try:
            col_a, row_values, distance_rows, row_idx = _live_sheet_read(config, date_str)
        except Exception as exc:
            return CascadeStep(kind="escalate", reason=f"sheet_read_failed:{type(exc).__name__}:{exc}")

    if not row_idx or row_idx < 1:
        return CascadeStep(kind="escalate", reason="date_row_not_found")

    ctx.accumulated.update({
        "date_str": date_str, "prospective": prospective, "events": events,
        "row_values": row_values, "row_idx": row_idx, "distance_rows": distance_rows,
    })
    return CascadeStep(kind="continue", reason=f"context_loaded:row={row_idx}",
                       metadata={"date_str": date_str, "row_idx": row_idx})


def not_flying_gate_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    row, events = ctx.accumulated.get("row_values"), ctx.accumulated.get("events", [])
    flew, why = ml.is_flight_day(row, events)
    if flew:
        ctx.accumulated["block_reason"] = why
        return CascadeStep(kind="escalate", reason=f"flight_day:{why}")
    relocated, rwhy = ml.is_relocation(row)
    if relocated:
        ctx.accumulated["block_reason"] = rwhy
        return CascadeStep(kind="escalate", reason=f"relocation:{rwhy}")
    ctx.accumulated["not_flying_evidence"] = why
    return CascadeStep(kind="continue", reason="not_flying_confirmed")


def resolve_distance_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    config = _cfg(ctx)
    home = config["home_label"]
    aliases = config.get("place_aliases", {})
    if ctx.inputs.get("destinations"):
        res = {"places": list(ctx.inputs["destinations"]), "events_used": [],
               "low_confidence": False, "too_many": len(ctx.inputs["destinations"]) > 2}
    else:
        res = ml.resolve_destinations(
            ctx.accumulated.get("events", []), home, aliases,
            utterance_hint=ctx.inputs.get("utterance"))
    if res["too_many"]:
        ctx.accumulated["block_reason"] = f"places={res['places']}"
        return CascadeStep(kind="escalate", reason="too_many_destinations")
    if not res["places"]:
        ctx.accumulated["ask_message"] = "I couldn't find where you drove — tell me the destination."
        ctx.accumulated["ask_missing"] = {"places": []}
        return CascadeStep(kind="escalate", reason="distance_ask")

    rt, leg = ml.parse_distance_tab(ctx.accumulated.get("distance_rows", []))
    provided = {"round_trip": ctx.inputs.get("provided_round_trip", {}),
                "leg": _coerce_leg(ctx.inputs.get("provided_leg", {}))}
    dist = ml.chained_loop_miles(res["places"], rt, leg, provided)
    if "ask" in dist:
        ctx.accumulated["ask_message"] = dist["ask"]
        ctx.accumulated["ask_missing"] = dist["missing"]
        return CascadeStep(kind="escalate", reason="distance_ask")

    ctx.accumulated.update({
        "places": res["places"], "events_used": res["events_used"],
        "low_confidence": res["low_confidence"], "miles": dist["miles"],
        "miles_breakdown": dist["breakdown"], "new_distance_entries": dist.get("new_entries", []),
    })
    return CascadeStep(kind="continue", reason=f"miles={dist['miles']}",
                       metadata={"miles": dist["miles"]})


def overwrite_gate_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    conflict = ml.row_conflict(ctx.accumulated.get("row_values"))
    if conflict and not ctx.inputs.get("confirm_overwrite"):
        ctx.accumulated["conflict_cols"] = conflict
        return CascadeStep(kind="escalate", reason=f"row_conflict:{','.join(conflict)}")
    ctx.accumulated["overwrote"] = bool(conflict)
    return CascadeStep(kind="continue", reason="overwrite_ok")


def build_draft_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    config = _cfg(ctx)
    business_pct = int(config.get("business_pct_default", 100))
    reason = ml.build_reason(
        ctx.accumulated["date_str"], ctx.accumulated["places"],
        ctx.accumulated.get("events_used", []), ctx.accumulated["miles_breakdown"],
        ctx.accumulated.get("not_flying_evidence", "no flight"),
        business_pct=business_pct, prospective=ctx.accumulated.get("prospective", False),
        low_confidence=ctx.accumulated.get("low_confidence", False))
    draft = ml.build_row_draft(
        ctx.accumulated["row_idx"], ctx.accumulated["miles"], reason,
        business_pct=business_pct,
        new_distance_entries=ctx.accumulated.get("new_distance_entries", []),
        prospective=ctx.accumulated.get("prospective", False),
        overwrote=ctx.accumulated.get("overwrote", False))
    draft["sheet_url"] = config["sheet_url"]
    draft["time_tracking_gid"] = config["time_tracking_gid"]
    draft["distance_gid"] = config["distance_gid"]
    ctx.accumulated["draft"] = draft
    return CascadeStep(kind="continue", reason="draft_built", metadata={"row": draft["row"]})


def register_handlers() -> None:
    """(Re)register this skill's rules handlers. Called at import; tests call it
    again because the global handler table is shared and other test modules
    clear it."""
    register_rules_handler(WORKFLOW_ID, "validate_inputs", validate_inputs_handler)
    register_rules_handler(WORKFLOW_ID, "read_context", read_context_handler)
    register_rules_handler(WORKFLOW_ID, "not_flying_gate", not_flying_gate_handler)
    register_rules_handler(WORKFLOW_ID, "resolve_distance", resolve_distance_handler)
    register_rules_handler(WORKFLOW_ID, "overwrite_gate", overwrite_gate_handler)
    register_rules_handler(WORKFLOW_ID, "build_draft", build_draft_handler)


register_handlers()


# ─── live connector helpers (lazy imports; only the production path) ─────

def _parse_today(value: Any) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    raise ValueError("inputs['today'] is required (inject it; Date.now is unavailable in this env)")


def _coerce_leg(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(k, tuple):
            out[k] = v
        elif "->" in str(k):
            a, b = str(k).split("->", 1)
            out[(a.strip(), b.strip())] = v
    return out


def _live_sheet_read(config: dict, date_str: str):
    from app.connectors.google_sheet import open_workbook, index_to_col_letter  # noqa
    wb = open_workbook(config["sheet_url"])
    tt = wb.get_worksheet_by_id(int(config["time_tracking_gid"]))
    col_a = tt.col_values(1)
    row_idx = ml.find_date_row(col_a, date_str)
    row_values = tt.row_values(row_idx) if row_idx else []
    row_values = (row_values + [""] * 10)[:10]
    dist = wb.get_worksheet_by_id(int(config["distance_gid"]))
    distance_rows = dist.get_values("A1:C200")
    return col_a, row_values, distance_rows, row_idx


def _live_calendar_events(date_str: str, config: dict) -> list[dict]:
    """Read the day's events through the Calendar connector (connector-isolation:
    no Google SDK in this skill). Fail-closed if auth/policy aren't wired."""
    from app.shared.config import get_settings  # noqa
    from app.control_plane.loaders import load_policy_document  # noqa
    from app.connectors.calendar import CalendarHistoryConnector  # noqa
    from app.connectors.calendar_auth import CalendarOAuthAuthenticator  # noqa
    settings = get_settings()
    policy = load_policy_document("meeting_decision")
    auth = CalendarOAuthAuthenticator(settings=settings, policy=policy)
    conn = CalendarHistoryConnector(authenticator=auth)
    return conn.list_events_on_date(date_str, tz_offset="-07:00")


# ─── public entry points ─────────────────────────────────────────────

def run(inputs: dict[str, Any], *, extra: dict[str, Any] | None = None) -> CascadeResult:
    manifest, report = load_skill_manifest(Path(__file__).parent)
    if not report.ok:
        raise RuntimeError(f"manifest invalid: {report.summary()}")
    return run_cascade(manifest=manifest, inputs=inputs, extra=extra or {})


def run_canary(case: dict) -> dict:
    """Fast eval: drive the REAL cascade on in-memory injected data and map the
    verdict to a compact outcome dict for comparison against `expected`."""
    inp = dict(case["input"])
    inp.setdefault("thread_id", case.get("case_id", "canary"))
    inp.setdefault("config", _canary_config(inp))
    with tempfile.TemporaryDirectory() as td:
        result = run(inp, extra={"proposed_dir": Path(td) / "proposed"})
    return _outcome(result)


def _canary_config(inp: dict) -> dict:
    return {
        "home_label": inp.get("home_label", ml.HOME_DEFAULT),
        "sheet_url": "https://example/edit", "time_tracking_gid": 1, "distance_gid": 2,
        "business_pct_default": 100, "kill_switch_env": "SAI_TRIP_MILEAGE_SEND_ENABLED",
        "place_aliases": inp.get("place_aliases", {}), "seed_distances": {},
    }


def _outcome(result: CascadeResult) -> dict:
    acc = result.accumulated
    if result.final_verdict == "ready_to_propose":
        d = acc.get("draft", {})
        return {"verdict": "propose", "miles": d.get("H"), "business_pct": d.get("I"),
                "places": acc.get("places", []), "prospective": d.get("prospective", False),
                "overwrote": d.get("overwrote", False)}
    if result.final_verdict == "no_op":
        return {"verdict": "no_op", "reason": result.final_reason}
    reason = result.final_reason
    if reason.startswith("flight_day"):
        return {"verdict": "blocked", "why": "flight"}
    if reason.startswith("relocation"):
        return {"verdict": "blocked", "why": "relocation"}
    if reason == "too_many_destinations":
        return {"verdict": "blocked", "why": "too_many"}
    if reason == "date_row_not_found":
        return {"verdict": "blocked", "why": "no_row"}
    if reason.startswith("row_conflict"):
        return {"verdict": "conflict", "cols": acc.get("conflict_cols", [])}
    if reason == "distance_ask":
        return {"verdict": "ask", "missing": acc.get("ask_missing", {}),
                "message": acc.get("ask_message", "")}
    return {"verdict": "escalate", "reason": reason}


def main() -> int:
    p = argparse.ArgumentParser(description="trip-mileage-log (read-only; stages an approval proposal).")
    p.add_argument("--utterance", required=True)
    p.add_argument("--today", required=True, help="YYYY-MM-DD (injected, no Date.now)")
    p.add_argument("--config", default=None)
    p.add_argument("--inject", default=None, help="path to a JSON file of injected events/sheet data (dry-run)")
    p.add_argument("--thread-id", default="cli")
    args = p.parse_args()

    inputs: dict[str, Any] = {"utterance": args.utterance, "today": args.today,
                              "config_path": args.config, "thread_id": args.thread_id}
    if args.inject:
        inputs.update(json.loads(Path(args.inject).read_text()))

    with tempfile.TemporaryDirectory() as td:
        result = run(inputs, extra={"proposed_dir": Path(td) / "proposed"})
        out = _outcome(result)
        print(json.dumps({"final_verdict": result.final_verdict,
                          "final_reason": result.final_reason,
                          "outcome": out,
                          "audit": [a["tier"] + ":" + a["kind"] for a in result.audit_log],
                          "draft": result.accumulated.get("draft")},
                         indent=2, default=str))
    return 0 if result.final_verdict in ("ready_to_propose",) else 1


if __name__ == "__main__":
    sys.exit(main())
