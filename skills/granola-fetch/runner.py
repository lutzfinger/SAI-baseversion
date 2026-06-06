"""Runner for granola-fetch v0.2.0 — SAI cascade-wired, autonomous.

Atomic skill. Fetches Granola meeting transcripts for a given folder +
date range and saves them as SAI-standard `<meeting_id>.json` files.

v0.2.0 (this version): uses `app.connectors.granola` directly — no
agent/MCP orchestration required. The cascade runs end-to-end as
deterministic Python and can be triggered from any context (slack
bot, email worker, scheduled task, CLI).

Public API
----------
- folder_match_handler              → resolve folder name → matching folders
- list_meetings_in_range_handler    → list notes in folder + date range
- fetch_each_transcript_handler     → pull each note's full transcript
- normalize_and_save_handler        → write <id>.json files; ready_to_propose
- run(inputs)                       → load manifest + run_cascade
- main()                            → CLI

Cascade inputs
--------------
  folder_name: str           required. Fuzzy-matched against folder list.
  date_range: str            required. "YYYY-MM-DD:YYYY-MM-DD" or "all".
  output_dir: str            required. Where <meeting_id>.json files go.
  granola_api_key: str       optional. If absent, resolved from env/op/keychain.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.cascade import (  # noqa: E402
    CascadeContext, CascadeResult, CascadeStep, register_rules_handler, run_cascade,
)
from app.connectors.granola import (  # noqa: E402
    GranolaPersonalAPIConnector, GranolaConnectorError, resolve_granola_api_key,
)
from app.skills.loader import load_skill_manifest  # noqa: E402


WORKFLOW_ID = "granola-fetch"


def _client_from_ctx(ctx: CascadeContext) -> GranolaPersonalAPIConnector:
    """Build the Granola client. Cache it on the ctx so subsequent tiers reuse it."""
    cached = ctx.accumulated.get("_granola_client")
    if cached is not None:
        return cached
    api_key = ctx.inputs.get("granola_api_key")
    try:
        client = GranolaPersonalAPIConnector(
            api_key=api_key or resolve_granola_api_key(),
        )
    except GranolaConnectorError as exc:
        # Re-raise as a CascadeStep escalate by storing the error in ctx
        ctx.accumulated["_granola_init_error"] = str(exc)
        raise
    ctx.accumulated["_granola_client"] = client
    return client


def _parse_date_range(date_range: str) -> tuple[Optional[str], Optional[str]]:
    """'2026-05-08:2026-05-15' → ('2026-05-08', '2026-05-15').
       'all' / '' → (None, None)."""
    if not date_range or date_range.strip().lower() in ("all", "all sessions", "*"):
        return None, None
    parts = date_range.strip().split(":")
    if len(parts) == 2 and all(p for p in parts):
        return parts[0].strip(), parts[1].strip()
    if len(parts) == 1 and parts[0].strip():
        return parts[0].strip(), parts[0].strip()
    return None, None


# ─── cascade tier handlers ────────────────────────────────────────────

def folder_match_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Light-touch folder validation — just records the operator-supplied
    name. Heavy folder lookup happens in list_meetings_in_range (which is
    cost-optimized: date-filter first, then folder-filter on survivors).
    This keeps the preflight cheap (no API calls)."""
    folder_name = (ctx.inputs.get("folder_name") or "").strip()
    if not folder_name:
        return CascadeStep(kind="escalate", reason="missing_input:folder_name")
    # Eagerly validate the connector can authenticate so we fail fast
    # if the key is missing — but we don't list folders yet.
    try:
        _client_from_ctx(ctx)
    except GranolaConnectorError as exc:
        return CascadeStep(kind="escalate", reason=f"granola_auth_failed:{exc}")
    return CascadeStep(
        kind="continue", reason=f"folder_name_recorded:{folder_name}",
        metadata={"folder_name_canonical": folder_name},
    )


def list_meetings_in_range_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """List notes in the folder, filtered by date range.

    Uses the connector's cost-optimized `list_notes_in_folder` which:
      1. Lists all notes (cheap metadata only).
      2. Date-filters first (no extra API calls).
      3. Fetches full details ONLY for date-passing candidates to read
         folder_membership.
    For typical 1-week runs this is ~10-30 GETs, not 1000.

    If zero notes match, returns no_op with a helpful list of folders
    that DO appear in the recent set so the operator can disambiguate.
    """
    folder_name = ctx.accumulated.get("folder_name_canonical") or ctx.inputs.get("folder_name")
    start_date, end_date = _parse_date_range(ctx.inputs.get("date_range", ""))
    client = _client_from_ctx(ctx)
    notes = client.list_notes_in_folder(folder_name, start_date=start_date, end_date=end_date)
    if not notes:
        # Diagnostic — let the operator see what IS in their recent set.
        # Bounded by max_notes_to_scan inside list_folders.
        try:
            recent_folders = client.list_folders(max_notes_to_scan=50)
            available = [f["name"] for f in recent_folders[:10]]
        except GranolaConnectorError:
            available = []
        return CascadeStep(
            kind="no_op",
            reason=(f"no_meetings_in_range:folder={folder_name!r}_"
                    f"start={start_date}_end={end_date}"),
            metadata={"available_folders": available},
        )
    meeting_list = [
        {"id": n.id, "title": n.title, "start_time": n.start_time}
        for n in notes
    ]
    return CascadeStep(
        kind="continue",
        reason=f"{len(meeting_list)}_meetings_to_fetch",
        metadata={
            "meeting_list": meeting_list,
            "date_map": {m["id"]: m["start_time"] for m in meeting_list},
        },
    )


def fetch_each_transcript_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Pull the full transcript for each meeting. Skips notes with null transcripts."""
    meeting_list = ctx.accumulated.get("meeting_list") or []
    if not meeting_list:
        return CascadeStep(kind="escalate", reason="precondition_failed:no_meeting_list")
    client = _client_from_ctx(ctx)
    fetched: list[dict[str, Any]] = []
    skipped_null: list[str] = []
    for m in meeting_list:
        try:
            note = client.get_note(note_id=m["id"])
        except GranolaConnectorError as exc:
            return CascadeStep(
                kind="escalate",
                reason=f"granola_fetch_failed:{m['id']}:{exc}",
                metadata={"failed_meeting_id": m["id"]},
            )
        ts = note.get("transcript")
        if ts is None or ts == "":
            skipped_null.append(m["id"])
            continue
        fetched.append({
            "meeting_id": m["id"],
            "title": m.get("title") or note.get("title", ""),
            "start_time": m["start_time"],
            "transcript": ts,
        })
    return CascadeStep(
        kind="continue",
        reason=f"fetched_{len(fetched)}_skipped_{len(skipped_null)}_null",
        metadata={
            "fetched_transcripts": fetched,
            "skipped_null": skipped_null,
        },
    )


def normalize_and_save_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Final tier — write each fetched transcript as a SAI-standard JSON file.
    Returns ready_to_propose with the saved list.
    """
    output_dir = ctx.inputs.get("output_dir")
    if not output_dir:
        return CascadeStep(kind="escalate", reason="missing_input:output_dir")
    fetched = ctx.accumulated.get("fetched_transcripts") or []
    if not fetched:
        return CascadeStep(kind="no_op", reason="no_transcripts_to_save")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for record in fetched:
        p = out_path / f"{record['meeting_id']}.json"
        p.write_text(json.dumps(record))
        saved.append(record["meeting_id"])

    return CascadeStep(
        kind="ready_to_propose",
        reason=f"saved_{len(saved)}_transcripts",
        metadata={
            "saved": saved,
            "skipped_null": ctx.accumulated.get("skipped_null", []),
            "output_dir": str(out_path),
        },
    )


# Register at import
register_rules_handler(WORKFLOW_ID, "folder_match", folder_match_handler)
register_rules_handler(WORKFLOW_ID, "list_meetings_in_range", list_meetings_in_range_handler)
register_rules_handler(WORKFLOW_ID, "fetch_each_transcript", fetch_each_transcript_handler)
register_rules_handler(WORKFLOW_ID, "normalize_and_save", normalize_and_save_handler)


# ─── public entry point ───────────────────────────────────────────────

def run(inputs: dict[str, Any]) -> CascadeResult:
    manifest, report = load_skill_manifest(Path(__file__).parent)
    if not report.ok:
        raise RuntimeError(f"manifest invalid: {report.summary()}")
    return run_cascade(manifest=manifest, inputs=inputs)


# ─── eval helpers (used by canary harness) ───────────────────────────

def run_canary(case: dict) -> str:
    ts = case.get("input", {}).get("transcript_field")
    if ts is None or ts == "":
        return "skip_no_audio"
    return "save"


def run_workflow_regression(case: dict) -> dict:
    """Synthetic workflow test — uses a fake client to avoid live API calls."""
    import tempfile
    fixtures = case["fixtures"]
    notes_by_id = {x["id"]: x for x in fixtures.get("tool_results", [])}
    date_map = fixtures.get("date_map", {})

    def fake_fetcher(url: str):
        if url.endswith("/notes") or "/notes?" in url:
            return {"notes": [
                {**n, "folder_membership": [{"name": "_TEST"}],
                 "created_at": date_map.get(n["id"], "2026-01-01T00:00:00Z")}
                for n in notes_by_id.values()
            ]}
        for nid, n in notes_by_id.items():
            if f"/notes/{nid}" in url:
                return {**n, "folder_membership": [{"name": "_TEST"}]}
        return {}

    client = GranolaPersonalAPIConnector(api_key="test", _fetcher=fake_fetcher)
    folders = client.list_folders()
    if not folders:
        return {"saved": [], "skipped_null": [], "skipped_unknown": []}
    notes = client.list_notes_in_folder(folders[0]["name"])
    saved, skipped_null, skipped_unknown = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        for n in notes:
            ts = n.raw.get("transcript")
            if ts is None or ts == "":
                skipped_null.append(n.id)
                continue
            if n.id not in date_map:
                skipped_unknown.append(n.id); continue
            (out_dir / f"{n.id}.json").write_text(json.dumps({
                "meeting_id": n.id, "title": n.title,
                "start_time": date_map[n.id], "transcript": ts,
            }))
            saved.append(n.id)
    return {"saved": saved, "skipped_null": skipped_null,
            "skipped_unknown": skipped_unknown}


# ─── CLI ─────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="granola-fetch (SAI atomic skill).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run", help="Run the cascade end-to-end.")
    sp.add_argument("--folder", required=True, help="Folder name (fuzzy).")
    sp.add_argument("--date-range", default="all",
                    help="'YYYY-MM-DD:YYYY-MM-DD' or 'all'.")
    sp.add_argument("--output-dir", required=True, type=Path)
    sp.add_argument("--thread-id", default="cli-run")
    args = p.parse_args()

    if args.cmd == "run":
        result = run({
            "folder_name": args.folder,
            "date_range": args.date_range,
            "output_dir": str(args.output_dir),
            "thread_id": args.thread_id,
        })
        print(json.dumps({
            "final_verdict": result.final_verdict,
            "final_reason": result.final_reason,
            "audit_log": result.audit_log,
            "proposal_path": result.proposal_path,
            "accumulated_summary": {
                k: v for k, v in result.accumulated.items()
                if k in ("folder_name_canonical", "meeting_count",
                         "saved", "skipped_null")
            },
        }, indent=2, default=str))
        return 0 if result.final_verdict == "ready_to_propose" else 1


if __name__ == "__main__":
    sys.exit(main())
