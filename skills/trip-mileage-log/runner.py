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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

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
    max_local = float(config.get("max_local_miles", 300))
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
        ctx.accumulated["block_reason"] = "no destination found in calendar"
        return CascadeStep(kind="escalate", reason="no_destination")

    rt, leg = ml.parse_distance_tab(ctx.accumulated.get("distance_rows", []))
    provided = {"round_trip": dict(ctx.inputs.get("provided_round_trip", {})),
                "leg": _coerce_leg(ctx.inputs.get("provided_leg", {}))}
    # Phase 2: auto-resolve any missing round-trip / leg via the distance
    # connector (Ship-1 primitive); cache them; NEVER ask the operator.
    try:
        _autoresolve_into(provided, res["places"], home, rt, leg,
                          ctx.extra.get("distance_connector"))
    except Exception as exc:  # DistanceUnavailable / network / parse — fail closed (#6)
        ctx.accumulated["block_reason"] = f"distance_unresolved:{type(exc).__name__}:{exc}"
        return CascadeStep(kind="escalate", reason="distance_unresolved")

    dist = ml.chained_loop_miles(res["places"], rt, leg, provided)
    if "ask" in dist:  # still missing (no connector available) → fail closed, no ask
        ctx.accumulated["block_reason"] = f"distance_unresolved:{dist.get('missing')}"
        return CascadeStep(kind="escalate", reason="distance_unresolved")
    miles = dist["miles"]
    if miles > max_local:  # deterministic plausibility bound (a flight, not a drive)
        ctx.accumulated["block_reason"] = f"loop {miles} mi exceeds max_local_miles {max_local}"
        return CascadeStep(kind="escalate", reason="distance_implausible")

    ctx.accumulated.update({
        "places": res["places"], "events_used": res["events_used"],
        "low_confidence": res["low_confidence"], "miles": miles,
        "miles_breakdown": dist["breakdown"], "new_distance_entries": dist.get("new_entries", []),
        "plausibility": f"loop {miles} mi <= max_local {max_local} (a drive)",
    })
    return CascadeStep(kind="continue", reason=f"miles={miles}", metadata={"miles": miles})


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
    # Context the safety gate reviews.
    draft["date_str"] = ctx.accumulated["date_str"]
    draft["places"] = ctx.accumulated.get("places", [])
    draft["route"] = " → ".join([config["home_label"]] + draft["places"] + [config["home_label"]])
    draft["not_flying_evidence"] = ctx.accumulated.get("not_flying_evidence", "")
    draft["plausibility"] = ctx.accumulated.get("plausibility", "ok")
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


# ─── distance auto-resolve (composes the Ship-1 connector) ──────────────

def _autoresolve_into(provided: dict, places: list, home: str, rt_tab: dict, leg_tab: dict, conn: Any) -> None:
    """Fill any missing round-trip / leg distances via the connector (mutates
    `provided`). Leaves a value missing only when no connector is available
    (caller then fails closed). Raises DistanceUnavailable on a resolve error."""
    prt = {ml._canon(k): v for k, v in provided["round_trip"].items()}
    for p in places:
        if ml._canon(p) in rt_tab or ml._canon(p) in prt:
            continue
        if conn is None:
            continue
        provided["round_trip"][p] = conn.round_trip_miles(home, p)
    if len(places) == 2 and conn is not None:
        a, b = places
        canon_legs = {(ml._canon(x), ml._canon(y)) for (x, y) in provided["leg"].keys()}
        have = (ml._leg_lookup(leg_tab, a, b) is not None
                or (ml._canon(a), ml._canon(b)) in canon_legs
                or (ml._canon(b), ml._canon(a)) in canon_legs)
        if not have:
            provided["leg"][(a, b)] = conn.leg_miles(a, b)


# ─── autonomous orchestrator (cornell run_autonomous pattern) ───────────

@dataclass
class PlanResult:
    ok: bool
    draft: dict | None
    blocked_reason: str
    accumulated: dict


@dataclass
class AutoResult:
    verdict: str            # wrote | refused | blocked | not_written
    reason: str
    wrote: bool = False
    draft: dict | None = None
    cells: dict | None = None
    safety_reason: str = ""


_DET_TIERS = (
    ("validate_inputs", validate_inputs_handler),
    ("read_context", read_context_handler),
    ("not_flying_gate", not_flying_gate_handler),
    ("resolve_distance", resolve_distance_handler),
    ("overwrite_gate", overwrite_gate_handler),
    ("build_draft", build_draft_handler),
)


def build_trip_plan(inputs: dict[str, Any], *, distance_connector: Any = None) -> PlanResult:
    """Run the deterministic tiers (no LLM, no write) → draft or a blocked
    reason. Split from the gate/write for testability."""
    ctx = CascadeContext(workflow_id=WORKFLOW_ID, inputs=dict(inputs))
    if distance_connector is not None:
        ctx.extra["distance_connector"] = distance_connector
    for _tier_id, handler in _DET_TIERS:
        step = handler(ctx, {})
        if step.kind in ("no_op", "escalate"):
            return PlanResult(False, None, step.reason, ctx.accumulated)
        ctx.accumulated.update(step.metadata)
    return PlanResult(True, ctx.accumulated.get("draft"), "", ctx.accumulated)


def _default_safety_reviewer(draft: dict) -> Any:
    import safety
    return safety.review_mileage_write(draft)


def run_autonomous(
    inputs: dict[str, Any],
    *,
    distance_connector: Any = None,
    safety_reviewer: Callable[[dict], Any] | None = None,
    ws_opener: Any = None,
    send_enabled: bool | None = None,
) -> AutoResult:
    """Plan → (deterministic plausibility, inside plan) → different-model safety
    gate → write. NO human in the loop. Fail-closed at every step."""
    plan = build_trip_plan(inputs, distance_connector=distance_connector)
    if not plan.ok:
        return AutoResult("blocked", plan.blocked_reason)
    draft = plan.draft
    reviewer = safety_reviewer or _default_safety_reviewer
    verdict = reviewer(draft)
    if not getattr(verdict, "safe", False):
        return AutoResult("refused", f"safety:{getattr(verdict, 'reason', '')}",
                          draft=draft, safety_reason=getattr(verdict, "reason", ""))
    import send_tool
    res = send_tool.apply_approved_proposal({"draft": draft}, ws_opener=ws_opener,
                                            send_enabled=send_enabled)
    return AutoResult("wrote" if res.wrote_sheet else "not_written", res.reason,
                      wrote=res.wrote_sheet, draft=draft, cells=res.cells)


def run(inputs: dict[str, Any], **kw: Any) -> AutoResult:  # back-compat alias
    return run_autonomous(inputs, **kw)


# ─── eval (drives the REAL autonomous path with injected stubs) ─────────

class _StubConnector:
    def __init__(self, distances: dict):
        self._d = {ml._canon(k): float(v) for k, v in (distances or {}).items()}

    def round_trip_miles(self, home: str, place: str) -> float:
        v = self._d.get(ml._canon(place))
        if v is None:
            from app.connectors.distance import DistanceUnavailable
            raise DistanceUnavailable(f"stub: no round-trip for {place!r}")
        return v

    def leg_miles(self, a: str, b: str) -> float:
        v = self._d.get(ml._canon(f"{a} -> {b}")) or self._d.get(ml._canon(f"{b} -> {a}"))
        if v is None:
            from app.connectors.distance import DistanceUnavailable
            raise DistanceUnavailable(f"stub: no leg {a}->{b}")
        return v


class _CaptureWS:
    def __init__(self):
        self.updates: list = []
        self.appends: list = []

    def acell(self, a1):
        from types import SimpleNamespace
        return SimpleNamespace(value="")

    def update(self, a1, values, value_input_option=None):
        self.updates.append((a1, values))

    def append_row(self, row, value_input_option=None):
        self.appends.append(row)


class _Verdict:
    def __init__(self, safe: bool, reason: str = ""):
        self.safe = safe
        self.reason = reason


def run_canary(case: dict) -> dict:
    """Drive run_autonomous hermetically: stub connector (from stub_distances),
    stub safety reviewer (unsafe iff input.unsafe), capturing fake worksheet,
    kill-switch forced on via send_enabled. NEVER a real sheet/network."""
    inp = dict(case["input"])
    inp.setdefault("thread_id", case.get("case_id", "canary"))
    inp.setdefault("config", _canary_config(inp))
    conn = _StubConnector(inp["stub_distances"]) if inp.get("stub_distances") is not None else None
    reviewer = (lambda d: _Verdict(False, "stub-unsafe")) if inp.get("unsafe") else (lambda d: _Verdict(True))
    ws = _CaptureWS()
    res = run_autonomous(inp, distance_connector=conn, safety_reviewer=reviewer,
                         ws_opener=lambda u, g: ws, send_enabled=True)
    return _auto_outcome(res, ws)


def _canary_config(inp: dict) -> dict:
    return {
        "home_label": inp.get("home_label", ml.HOME_DEFAULT),
        "sheet_url": "https://example/edit", "time_tracking_gid": 1, "distance_gid": 2,
        "business_pct_default": 100, "kill_switch_env": "SAI_TRIP_MILEAGE_SEND_ENABLED",
        "place_aliases": inp.get("place_aliases", {}), "seed_distances": {},
        "max_local_miles": inp.get("max_local_miles", 300),
    }


_BLOCK_WHY = {
    "too_many_destinations": "too_many", "date_row_not_found": "no_row",
    "no_destination": "no_destination", "distance_unresolved": "unresolved",
    "distance_implausible": "implausible",
}


def _auto_outcome(res: AutoResult, ws: Any) -> dict:
    if res.verdict == "wrote":
        d = res.draft or {}
        return {"verdict": "wrote", "wrote": True, "miles": d.get("H"),
                "business_pct": d.get("I"), "places": d.get("places", []),
                "prospective": d.get("prospective", False), "overwrote": d.get("overwrote", False)}
    if res.verdict == "refused":
        return {"verdict": "refused", "wrote": False}
    if res.verdict == "blocked":
        r = res.reason
        if r.startswith("flight_day"):
            why = "flight"
        elif r.startswith("relocation"):
            why = "relocation"
        elif r.startswith("row_conflict"):
            why = "conflict"
        else:
            why = _BLOCK_WHY.get(r, r)
        return {"verdict": "blocked", "why": why, "wrote": False}
    return {"verdict": res.verdict, "wrote": res.wrote}


def main() -> int:
    p = argparse.ArgumentParser(description="trip-mileage-log autonomous runner.")
    p.add_argument("--utterance", required=True)
    p.add_argument("--today", required=True, help="YYYY-MM-DD (injected)")
    p.add_argument("--config", default=None)
    p.add_argument("--inject", default=None, help="JSON file of injected events/sheet data (dry-run)")
    p.add_argument("--thread-id", default="cli")
    p.add_argument("--write", action="store_true", help="actually write (else dry-run; kill-switch still applies)")
    args = p.parse_args()
    inputs: dict[str, Any] = {"utterance": args.utterance, "today": args.today,
                              "config_path": args.config, "thread_id": args.thread_id}
    if args.inject:
        inputs.update(json.loads(Path(args.inject).read_text()))
    conn = None
    if "stub_distances" not in inputs:
        from app.connectors.distance import DistanceConnector
        cfg = inputs.get("config") or load_trip_config(args.config)
        inputs.setdefault("config", cfg)
        conn = DistanceConnector.from_config(cfg.get("distance"))
    res = run_autonomous(inputs, distance_connector=conn, send_enabled=(args.write or None))
    print(json.dumps({"verdict": res.verdict, "reason": res.reason, "wrote": res.wrote,
                      "draft": res.draft}, indent=2, default=str))
    return 0 if res.verdict in ("wrote", "refused") else 1


if __name__ == "__main__":
    sys.exit(main())
