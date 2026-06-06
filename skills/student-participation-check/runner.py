"""Runner for student-participation-check v0.1.0 — composed SAI workflow.

Demonstrates the atomic-reuse pattern: this composed skill imports
handler functions from the atomic skills (`fuzzy-name-count`) and
registers them under its OWN workflow_id, so the same atomic step
participates in a different plan.

Plan (5 tiers):
  1. validate_inputs     — own rules handler; checks transcripts dir + sheet|file
  2. read_roster         — own rules handler; pulls names from sheet or file
  3. count_callouts      — REUSES fuzzy-name-count handlers (load + alias + count chained)
  4. append_sheet_columns_or_csv — own rules handler; writes the matrix
  5. crosscheck_and_finalize     — own rules handler; verifies invariant, ready_to_propose
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.cascade import (  # noqa: E402
    CascadeContext, CascadeResult, CascadeStep, register_rules_handler, run_cascade,
)
from app.shared.fuzzy_match import (  # noqa: E402
    load_transcripts, name_aliases, count_callouts,
)
from app.connectors.google_sheet import (  # noqa: E402
    open_sheet, col_letter_to_index, index_to_col_letter,
)
from app.skills.loader import load_skill_manifest  # noqa: E402

# Reuse handlers from the atomic skills — composed workflows depend on atomics.
# Both atomic-skill runners are loaded via importlib.util (their directory
# names contain hyphens, so they can't be plain `import` targets).
import importlib.util as _ilu  # noqa: E402
import sys as _sys             # noqa: E402

def _load_atomic(name: str) -> Any:
    mod_name = f"_atomic_{name.replace('-', '_')}"
    if mod_name in _sys.modules:
        return _sys.modules[mod_name]
    path = _SAI_ROOT / "skills" / name / "runner.py"
    spec = _ilu.spec_from_file_location(mod_name, path)
    mod = _ilu.module_from_spec(spec)
    _sys.modules[mod_name] = mod                # needed for @dataclass etc.
    spec.loader.exec_module(mod)
    return mod

_fnc_module = _load_atomic("fuzzy-name-count")
_granola_fetch_module = _load_atomic("granola-fetch")


WORKFLOW_ID = "student-participation-check"


# ─── tier handlers (own) ─────────────────────────────────────────────

def validate_inputs_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    folder_name = ctx.inputs.get("folder_name") or ctx.inputs.get("folder")
    sheet_url = ctx.inputs.get("sheet_url")
    students_file = ctx.inputs.get("students_file")
    transcripts_dir = ctx.inputs.get("transcripts_dir")  # back-compat: caller may pre-fetch
    if not folder_name and not transcripts_dir:
        return CascadeStep(
            kind="escalate",
            reason="missing_input:need folder_name (to fetch from Granola) or transcripts_dir (pre-fetched)",
        )
    if not sheet_url and not students_file:
        return CascadeStep(kind="escalate", reason="missing_input:need sheet_url or students_file")
    return CascadeStep(kind="continue", reason="inputs_validated",
                       metadata={"folder_name": folder_name or "(pre-fetched)"})


def granola_fetch_via_atomic_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """REUSE the granola-fetch atomic. If caller already supplied a
    transcripts_dir, skip Granola entirely (back-compat / dev mode)."""
    transcripts_dir = ctx.inputs.get("transcripts_dir")
    if transcripts_dir and Path(transcripts_dir).is_dir():
        return CascadeStep(
            kind="continue",
            reason=f"transcripts_dir_supplied:skipping_granola_fetch",
            metadata={"transcripts_dir": str(transcripts_dir)},
        )
    folder_name = ctx.inputs.get("folder_name") or ctx.inputs.get("folder")
    date_range = ctx.inputs.get("date_range", "all")
    # Default output dir lives in the per-skill private SAI dir.
    default_out = (Path.home() / ".SAI" / "student-participation-check"
                   / "transcripts" / ctx.inputs.get("thread_id", "anon"))
    output_dir = ctx.inputs.get("granola_output_dir") or str(default_out)

    sub_inputs = {
        "folder_name": folder_name,
        "date_range": date_range,
        "output_dir": output_dir,
        "granola_api_key": ctx.inputs.get("granola_api_key"),
    }
    sub_ctx = CascadeContext(workflow_id="granola-fetch", inputs=sub_inputs)
    # Chain the atomic's 4 handlers manually
    handlers = (
        _granola_fetch_module.folder_match_handler,
        _granola_fetch_module.list_meetings_in_range_handler,
        _granola_fetch_module.fetch_each_transcript_handler,
        _granola_fetch_module.normalize_and_save_handler,
    )
    for h in handlers:
        try:
            step = h(sub_ctx, {})
        except Exception as exc:
            return CascadeStep(kind="escalate",
                               reason=f"granola_fetch_crashed:{type(exc).__name__}:{exc}")
        if step.kind in ("no_op", "escalate"):
            return CascadeStep(kind=step.kind,
                               reason=f"delegated_to_granola_fetch:{step.reason}",
                               metadata=step.metadata)
        sub_ctx.accumulated.update(step.metadata)
    # Last handler returned ready_to_propose with `saved` + `output_dir`.
    saved_ids = sub_ctx.accumulated.get("saved", [])
    skipped = sub_ctx.accumulated.get("skipped_null", [])
    return CascadeStep(
        kind="continue",
        reason=f"granola_fetched:{len(saved_ids)}_saved_{len(skipped)}_null_audio",
        metadata={
            "transcripts_dir": sub_ctx.accumulated.get("output_dir"),
            "granola_saved": saved_ids,
            "granola_skipped_null": skipped,
            "folder_name_canonical": sub_ctx.accumulated.get("folder_name_canonical"),
        },
    )


def read_roster_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Read the student roster from either the Google Sheet (column A by
    default) or a local text file. Stores the names + sheet row indices
    in accumulated state for later tiers."""
    students_file = ctx.inputs.get("students_file")
    sheet_url = ctx.inputs.get("sheet_url")
    name_column = ctx.inputs.get("name_column", "A")
    worksheet_name = ctx.inputs.get("worksheet")
    header_row = 1
    student_rows: list[tuple[int, str]] = []
    ws_meta: dict[str, Any] = {}

    if students_file:
        names = [ln.strip() for ln in Path(students_file).read_text().splitlines() if ln.strip()]
        for i, val in enumerate(names, start=header_row + 1):
            if name_aliases(val):
                student_rows.append((i, val))
    else:
        ws = open_sheet(sheet_url, worksheet_name)
        name_col_idx = col_letter_to_index(name_column)
        name_col_values = ws.col_values(name_col_idx + 1)
        for i, val in enumerate(name_col_values[header_row:], start=header_row + 1):
            if val and val.strip() and name_aliases(val.strip()):
                student_rows.append((i, val.strip()))
        ws_meta = {"worksheet_name": ws.title, "existing_header_len": len(ws.row_values(header_row))}

    if not student_rows:
        return CascadeStep(kind="no_op", reason="no_usable_names_in_roster")

    return CascadeStep(
        kind="continue", reason=f"roster_loaded:{len(student_rows)}_students",
        metadata={
            "student_rows": student_rows,
            "names": [n for _, n in student_rows],
            "ws_meta": ws_meta,
        },
    )


def count_callouts_via_atomic_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """REUSE the fuzzy-name-count atomic. Manually chain its three core
    tiers (load, aliases, count) so the composed cascade audit log
    records that we delegated to the atomic.
    """
    names = ctx.accumulated.get("names", [])
    transcripts_dir = ctx.inputs.get("transcripts_dir")
    threshold = ctx.inputs.get("threshold", 85)
    any_speaker = ctx.inputs.get("any_speaker", False)

    # The granola_fetch tier (or back-compat input) places transcripts_dir
    # in accumulated state; prefer it over the raw input.
    transcripts_dir = (ctx.accumulated.get("transcripts_dir")
                       or ctx.inputs.get("transcripts_dir"))
    # Build a sub-context mirroring fuzzy-name-count's input shape
    sub_inputs = {
        "transcripts_dir": transcripts_dir,
        "names": names,
        "threshold": threshold,
        "any_speaker": any_speaker,
    }
    sub_ctx = CascadeContext(workflow_id="fuzzy-name-count", inputs=sub_inputs)

    # Chain the atomic's tier handlers manually
    for tier_handler in (
        _fnc_module.load_transcripts_handler,
        _fnc_module.build_aliases_handler,
        _fnc_module.identify_target_speaker_handler,
        _fnc_module.count_callouts_handler,
    ):
        step = tier_handler(sub_ctx, {})
        if step.kind in ("no_op", "escalate"):
            return CascadeStep(kind=step.kind,
                               reason=f"delegated_to_fuzzy_name_count:{step.reason}")
        sub_ctx.accumulated.update(step.metadata)

    return CascadeStep(
        kind="continue", reason="counted_via_fuzzy_name_count_atomic",
        metadata={
            "per_session_counts": sub_ctx.accumulated["per_session_counts"],
            "session_headers": sub_ctx.accumulated["session_headers"],
            "transcripts_count": len(sub_ctx.accumulated["transcripts"]),
        },
    )


def _session_header(start_time_iso: str, meeting_id: str = "") -> str:
    if start_time_iso:
        return f"session - {start_time_iso[:10]}"
    return f"session - {meeting_id[:10]}"


def build_matrix_and_crosscheck_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Build the matrix in memory, append the logfile (local audit), and
    verify the invariant. Does NOT write to the Google Sheet — that's
    gated by the `human` tier + send_tool.py per SAI policy on external
    mutations (PRINCIPLES.md §2/§9)."""
    student_rows = ctx.accumulated.get("student_rows") or []
    per_session = ctx.accumulated.get("per_session_counts") or []
    raw_headers = ctx.accumulated.get("session_headers") or []
    headers = [f"session - {h}" if not h.startswith("session - ") else h for h in raw_headers]
    cols = len(headers)
    n_t = ctx.accumulated.get("transcripts_count", 0)

    # Crosscheck — bail before staging anything if invariant fails.
    if cols != n_t:
        return CascadeStep(
            kind="escalate",
            reason=f"crosscheck_failed:{cols}_columns_vs_{n_t}_transcripts",
        )

    # Build the matrix preview
    new_column_count = cols + 1  # + Total
    matrix: list[list] = [headers + ["Total Callouts"]]
    cur_row = 2
    for student_idx, (sheet_row, _) in enumerate(student_rows):
        while cur_row < sheet_row:
            matrix.append([""] * new_column_count); cur_row += 1
        row_counts = [per_session[s][student_idx] for s in range(cols)]
        row_counts.append(sum(row_counts))
        matrix.append(row_counts); cur_row += 1

    # Local CSV mirror (always written when --output-csv given; no approval needed)
    csv_path_str = None
    output_csv = ctx.inputs.get("output_csv")
    if output_csv:
        p = Path(output_csv); p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Name"] + headers + ["Total Callouts"])
            data_rows = [r for r in matrix[1:] if r and isinstance(r[-1], int)]
            for (_, full_name), row in zip(student_rows, data_rows):
                w.writerow([full_name] + list(row))
        csv_path_str = str(p)

    # Append local logfile (append-only audit, per §4 — local only, no external side effect)
    logfile = ctx.inputs.get("logfile")
    if logfile:
        folder = ctx.inputs.get("folder", "(unspecified)")
        date_range = ctx.inputs.get("date_range", "(unspecified)")
        lp = Path(logfile); lp.parent.mkdir(parents=True, exist_ok=True)
        new_file = not lp.exists()
        with lp.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["run_timestamp", "workflow_id", "folder", "date_range",
                            "session_columns", "transcripts_loaded", "student_count"])
            w.writerow([datetime.now().isoformat(timespec="seconds"),
                        WORKFLOW_ID, folder, date_range, cols, n_t, len(student_rows)])

    # Stash everything the `human` tier (and downstream send_tool) needs.
    # The `human` tier handler in app/cascade/runner.py promotes well-known
    # keys to the top level of the staged proposal YAML; anything we put
    # under `draft` ends up directly in the proposal_body.draft field.
    return CascadeStep(
        kind="continue",
        reason=f"matrix_built:{cols}_cols_x_{len(student_rows)}_students",
        metadata={
            # `draft` is the standard proposal-body key — see _handle_human
            "draft": {
                "workflow_id": WORKFLOW_ID,
                "matrix_preview": matrix,
                "headers": headers + ["Total Callouts"],
                "new_session_columns": cols,
                "transcripts_loaded": n_t,
                "student_count": len(student_rows),
                "sheet_url": ctx.inputs.get("sheet_url"),
                "worksheet": ctx.inputs.get("worksheet"),
                "name_column": ctx.inputs.get("name_column", "A"),
                "folder": ctx.inputs.get("folder", ""),
                "date_range": ctx.inputs.get("date_range", ""),
                "student_rows": student_rows,
                "csv_path": csv_path_str,
            },
            "new_session_columns": cols,
            "transcripts_count": n_t,
            "csv_path": csv_path_str,
        },
    )


# Register all rules tiers; `human` is a built-in framework handler.
register_rules_handler(WORKFLOW_ID, "validate_inputs", validate_inputs_handler)
register_rules_handler(WORKFLOW_ID, "granola_fetch", granola_fetch_via_atomic_handler)
register_rules_handler(WORKFLOW_ID, "read_roster", read_roster_handler)
register_rules_handler(WORKFLOW_ID, "count_callouts", count_callouts_via_atomic_handler)
register_rules_handler(WORKFLOW_ID, "build_matrix_and_crosscheck", build_matrix_and_crosscheck_handler)


# ─── public entry point ─────────────────────────────────────────────

def run(inputs: dict[str, Any]) -> CascadeResult:
    manifest, report = load_skill_manifest(Path(__file__).parent)
    if not report.ok:
        raise RuntimeError(f"manifest invalid: {report.summary()}")
    return run_cascade(manifest=manifest, inputs=inputs)


def run_canary(case: dict) -> dict:
    """Fast eval — runs the atomic chain on in-memory transcript fixtures."""
    inp = case["input"]
    raw_ts = inp["transcripts"]
    names = inp["names"]
    aliases = [name_aliases(n) for n in names if name_aliases(n)]
    from app.shared.fuzzy_match import parse_granola_transcript_string

    transcripts = []
    for t in raw_ts:
        if t.get("transcript") is None or t.get("transcript") == "":
            continue
        if isinstance(t.get("transcript"), str):
            t = {**t, "transcript": parse_granola_transcript_string(t["transcript"])}
            if not t["transcript"]:
                continue
        transcripts.append(t)
    transcripts.sort(key=lambda t: t.get("start_time") or "")

    if not aliases:
        return {"headers": [], "matrix": [], "crosscheck_pass": True,
                "new_session_columns": 0, "transcripts_loaded": 0}

    headers = [_session_header(t.get("start_time", ""), t.get("meeting_id", "")) for t in transcripts]
    per_session = [count_callouts(t, aliases, inp.get("threshold", 85),
                                   inp.get("any_speaker", False)) for t in transcripts]
    matrix = []
    for i, _ in enumerate(aliases):
        row = [per_session[s][i] for s in range(len(transcripts))]
        row.append(sum(row))
        matrix.append(row)

    return {
        "headers": headers + ["Total Callouts"], "matrix": matrix,
        "new_session_columns": len(headers), "transcripts_loaded": len(transcripts),
        "crosscheck_pass": len(headers) == len(transcripts),
    }


# ─── CLI ────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="student-participation-check (composed SAI workflow).")
    p.add_argument("--transcripts", required=True, type=Path)
    p.add_argument("--sheet", default=None)
    p.add_argument("--students-file", type=Path, default=None)
    p.add_argument("--name-column", default="A")
    p.add_argument("--worksheet", default=None)
    p.add_argument("--threshold", type=int, default=85)
    p.add_argument("--any-speaker", action="store_true")
    p.add_argument("--logfile", type=Path,
                   default=Path.home() / "Claude-Logs/SAI/student-participation-check/log.csv")
    p.add_argument("--folder", default="(unspecified)")
    p.add_argument("--date-range", default="(unspecified)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output-csv", type=Path, default=None)
    args = p.parse_args()

    inputs = {
        "transcripts_dir": str(args.transcripts),
        "sheet_url": args.sheet,
        "students_file": str(args.students_file) if args.students_file else None,
        "name_column": args.name_column,
        "worksheet": args.worksheet,
        "threshold": args.threshold,
        "any_speaker": args.any_speaker,
        "logfile": str(args.logfile),
        "folder": args.folder,
        "date_range": args.date_range,
        "dry_run": args.dry_run,
        "output_csv": str(args.output_csv) if args.output_csv else None,
    }

    result = run(inputs)
    print(json.dumps({
        "final_verdict": result.final_verdict,
        "final_reason": result.final_reason,
        "audit_log": result.audit_log,
        "summary": {
            "new_session_columns": result.accumulated.get("new_session_columns"),
            "transcripts_loaded": result.accumulated.get("transcripts_count"),
            "sheet_a1": result.accumulated.get("sheet_a1"),
            "csv_path": result.accumulated.get("csv_path"),
        },
    }, indent=2, default=str))
    return 0 if result.final_verdict == "ready_to_propose" else 1


if __name__ == "__main__":
    sys.exit(main())
