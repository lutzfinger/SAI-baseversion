"""Apply an APPROVED trip-mileage-log proposal — the ONLY place a sheet write
happens (PRINCIPLES §2/§9: policy before side effects).

Fires after the operator ✅ on the staged proposal YAML. Per §16e it sits behind
a kill-switch env var that DEFAULTS OFF; per #6 it re-validates fail-closed and
refuses on anything off. Writes H (miles), I (% business), J (reason) to the
date row, and appends any new round-trip distances to the "Distance MTV to" tab.

`ws_opener` is injectable so tests exercise the write logic against a fake
worksheet with no live sheet.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

KILL_SWITCH_ENV = "SAI_TRIP_MILEAGE_SEND_ENABLED"
WORKFLOW_ID = "trip-mileage-log"

log = logging.getLogger("sai.trip-mileage-log.send_tool")


@dataclass
class ApplyResult:
    wrote_sheet: bool
    cells: dict[str, Any] = field(default_factory=dict)
    distance_appended: list[dict] = field(default_factory=list)
    reason: str = ""


def _default_opener(sheet_url: str, gid: int):
    from app.connectors.google_sheet import open_workbook  # noqa
    return open_workbook(sheet_url).get_worksheet_by_id(int(gid))


def apply_approved_proposal(
    proposal_body: dict[str, Any],
    *,
    ws_opener: Optional[Callable[[str, int], Any]] = None,
) -> ApplyResult:
    """Write an operator-approved proposal to the Google Sheet. Fail-closed on
    every off-shape input; no-op (no write) when the kill switch is off."""
    draft = proposal_body.get("draft") or proposal_body
    if draft.get("workflow_id") != WORKFLOW_ID:
        return ApplyResult(False, reason="wrong_workflow_id_in_proposal")

    row = draft.get("row")
    miles = draft.get("H")
    business = draft.get("I")
    reason_text = draft.get("J")
    if not isinstance(row, int) or row < 2:
        return ApplyResult(False, reason=f"bad_row:{row!r}")
    try:
        miles = float(miles)
    except (TypeError, ValueError):
        return ApplyResult(False, reason=f"bad_miles:{miles!r}")
    if miles <= 0:
        return ApplyResult(False, reason=f"non_positive_miles:{miles}")
    if not isinstance(business, int) or not (0 <= business <= 100):
        return ApplyResult(False, reason=f"bad_business_pct:{business!r}")
    if not reason_text:
        return ApplyResult(False, reason="empty_reason")

    # Kill switch — defaults OFF (§16e). Operator enables explicitly after a
    # green dry-run.
    if os.environ.get(KILL_SWITCH_ENV, "0") != "1":
        log.info("kill_switch_off — would have written H%d=%s", row, miles)
        return ApplyResult(False, cells={"H": miles, "I": business},
                           reason=f"kill_switch_off:set_{KILL_SWITCH_ENV}=1_to_enable")

    opener = ws_opener or _default_opener
    sheet_url = draft.get("sheet_url")
    tt = opener(sheet_url, draft.get("time_tracking_gid"))

    # Defense in depth: refuse to overwrite a populated cell unless the proposal
    # explicitly recorded an operator-confirmed overwrite.
    existing = {c: str((tt.acell(f"{c}{row}").value if hasattr(tt, "acell") else "") or "")
                for c in ("H", "I", "J")} if not draft.get("overwrote") else {}
    if not draft.get("overwrote"):
        populated = [c for c, v in existing.items() if v.strip()]
        if populated:
            return ApplyResult(False, reason=f"unconfirmed_overwrite:{','.join(populated)}")

    tt.update(f"H{row}", [[miles]], value_input_option="USER_ENTERED")
    tt.update(f"I{row}", [[business]], value_input_option="USER_ENTERED")
    tt.update(f"J{row}", [[reason_text]], value_input_option="USER_ENTERED")

    appended: list[dict] = []
    new_entries = draft.get("new_distance_entries") or []
    if new_entries:
        dist = opener(sheet_url, draft.get("distance_gid"))
        for entry in new_entries:
            dist.append_row([entry["name"], entry["miles"]], value_input_option="USER_ENTERED")
            appended.append(entry)

    log.info("wrote_sheet row=%d H=%s I=%s", row, miles, business)
    return ApplyResult(True, cells={"H": miles, "I": business, "J": reason_text},
                       distance_appended=appended, reason="ok")


if __name__ == "__main__":
    import yaml
    if len(sys.argv) != 2:
        print("usage: python send_tool.py <approved-proposal.yaml>")
        sys.exit(2)
    body = yaml.safe_load(Path(sys.argv[1]).read_text())
    res = apply_approved_proposal(body)
    print(f"wrote_sheet: {res.wrote_sheet}")
    print(f"cells:       {res.cells}")
    print(f"appended:    {res.distance_appended}")
    print(f"reason:      {res.reason}")
    sys.exit(0 if res.wrote_sheet else 1)
