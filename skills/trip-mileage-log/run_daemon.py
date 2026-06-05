"""Headless poll cycle for the trip-mileage email daemon (cornell run_once
pattern). `run_once(...)` is the testable orchestrator — ALL I/O (gmail,
calendar+sheet read, distance, safety, reply, clock) is injected. `main()`
wires the real ones and is the launchd entry. Security envelope:
operator-allowlisted sender only · reply only to the operator · per-day cap ·
attempted+processed markers (fire once) · every fail-closed path → a "needs
human" reply + processed.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake                       # noqa: E402
import mileage_logic as ml          # noqa: E402
from runner import run_autonomous   # noqa: E402

PROCESSED_LABEL = "SAI/trip_mileage_processed"
ATTEMPTED_LABEL = "SAI/trip_mileage_attempted"


@dataclass
class ThreadOutcome:
    thread_id: str
    status: str            # wrote | needs_human | ignored | skipped_cap
    detail: str = ""


@dataclass
class RunResult:
    outcomes: list = field(default_factory=list)
    skipped_reason: str = ""


def run_once(
    *,
    gmail: Any,
    fetch_day_context: Callable[[str], tuple],
    config: dict,
    reply_sender: Callable[[dict, str], None],
    now: date,
    distance_connector: Any = None,
    safety_reviewer: Optional[Callable[[dict], Any]] = None,
    ws_opener: Any = None,
    send_enabled: Optional[bool] = None,
) -> RunResult:
    """One poll cycle. `gmail` provides count_processed_today / find_unprocessed_threads
    / get_thread_meta / add_label. `fetch_day_context(date_str)` returns
    (events, sheet_col_a, row_values, distance_rows)."""
    result = RunResult()
    cap = int(config.get("max_writes_per_day", 5))
    already = gmail.count_processed_today(PROCESSED_LABEL)
    if cap - already <= 0:
        result.skipped_reason = f"per-day cap reached ({already}/{cap})"
        return result
    remaining = cap - already
    operator_addresses = config.get("operator_addresses", [])

    for thread_id in gmail.find_unprocessed_threads(PROCESSED_LABEL):
        if remaining <= 0:
            result.outcomes.append(ThreadOutcome(thread_id, "skipped_cap"))
            continue
        meta = gmail.get_thread_meta(thread_id)

        # Fail-closed sender validation: act ONLY on an operator request.
        req = intake.parse_trigger_email(meta, operator_addresses=operator_addresses)
        if req is None:
            gmail.add_label(thread_id, PROCESSED_LABEL)   # fire once; NO reply to a non-operator
            result.outcomes.append(ThreadOutcome(thread_id, "ignored", "non-operator sender or no trip statement"))
            continue

        date_str, _prospective = ml.parse_trip_date(req.utterance, now)
        if not date_str:
            _needs_human(gmail, reply_sender, meta, thread_id, "could not parse the trip date")
            result.outcomes.append(ThreadOutcome(thread_id, "needs_human", "unparseable_date"))
            continue
        try:
            events, col_a, row_values, distance_rows = fetch_day_context(date_str)
        except Exception as exc:  # headless must never hang/crash on a read error
            _needs_human(gmail, reply_sender, meta, thread_id, f"could not read calendar/sheet: {exc}")
            result.outcomes.append(ThreadOutcome(thread_id, "needs_human", f"context_read:{type(exc).__name__}"))
            continue

        inputs = {"utterance": req.utterance, "today": now, "config": config,
                  "events": events, "sheet_col_a": col_a, "row_values": row_values,
                  "distance_rows": distance_rows, "thread_id": thread_id}
        gmail.add_label(thread_id, ATTEMPTED_LABEL)       # pre-write marker
        res = run_autonomous(inputs, distance_connector=distance_connector,
                             safety_reviewer=safety_reviewer, ws_opener=ws_opener,
                             send_enabled=send_enabled)
        if res.wrote:
            reply_sender(meta, _wrote_body(res.draft or {}))
            gmail.add_label(thread_id, PROCESSED_LABEL)
            result.outcomes.append(ThreadOutcome(thread_id, "wrote", f"H={(res.draft or {}).get('H')}"))
            remaining -= 1
        else:
            _needs_human(gmail, reply_sender, meta, thread_id, f"{res.verdict}: {res.reason}")
            result.outcomes.append(ThreadOutcome(thread_id, "needs_human", f"{res.verdict}:{res.reason}"))
    return result


def _needs_human(gmail: Any, reply_sender: Callable[[dict, str], None], meta: dict, thread_id: str, reason: str) -> None:
    reply_sender(meta, (
        "SAI did NOT log this trip — it needs a human.\n"
        f"Reason (fail-closed): {reason}\n"
        "Nothing was written. The thread is marked processed so SAI fires once.\n— SAI"
    ))
    gmail.add_label(thread_id, PROCESSED_LABEL)


def _wrote_body(d: dict) -> str:
    return (
        "SAI logged your trip to the mileage sheet.\n"
        f"Row {d.get('row')}: H={d.get('H')} miles · I={d.get('I')}% business.\n"
        f"Reason: {d.get('J')}\n— SAI"
    )


def main() -> int:  # pragma: no cover - live wiring only
    """Launchd entry: wire real Gmail (operator token), live calendar+sheet
    reads, the distance connector, the real safety gate, and a threaded reply."""
    from datetime import date as _date

    from trip_config import load_trip_config
    from app.connectors.distance import DistanceConnector
    import runner as _runner

    config = load_trip_config(None)
    connector = DistanceConnector.from_config(config.get("distance"))

    def fetch_day_context(date_str: str):
        col_a, row_values, distance_rows, _row_idx = _runner._live_sheet_read(config, date_str)
        events = _runner._live_calendar_events(date_str, config)
        return events, col_a, row_values, distance_rows

    # gmail + reply_sender wiring left to the operator's runtime env (dedicated
    # token, From: sai@, To: operator only). Intentionally not constructed here
    # until the daemon is installed.
    raise SystemExit("run_daemon.main: wire gmail + reply_sender for the installed daemon")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
