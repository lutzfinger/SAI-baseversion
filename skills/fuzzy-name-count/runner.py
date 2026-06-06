"""Runner for fuzzy-name-count v0.1.0 — SAI cascade-wired.

Atomic skill. Counts fuzzy first-name mentions in a directory of transcripts.

The skill.yaml cascade is 5 tiers. Each tier is a registered rules
handler — composed workflows can import any of these handlers and
register them under their own workflow_id to reuse the same atomic
step in a different plan.

Public handlers (importable + reusable in composed workflows)
-------------------------------------------------------------
- load_transcripts_handler         → reads JSON files into ctx.accumulated['transcripts']
- build_aliases_handler            → builds per-person alias lists
- identify_target_speaker_handler  → noop tier (per-transcript work; documents the rule)
- count_callouts_handler           → builds the count matrix
- emit_csv_handler                 → writes the CSV (final tier — ready_to_propose)

The cascade ends with `ready_to_propose` carrying the matrix so the
SAI orchestrator can surface the result. For pure CLI mode the runner
also writes the CSV directly.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

_SAI_ROOT = Path(__file__).resolve().parents[2]
if str(_SAI_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAI_ROOT))

from app.cascade import (  # noqa: E402
    CascadeContext, CascadeResult, CascadeStep, register_rules_handler, run_cascade,
)
from app.shared.fuzzy_match import (  # noqa: E402
    load_transcripts as _lib_load_transcripts,
    name_aliases, count_callouts,
)
from app.skills.loader import load_skill_manifest  # noqa: E402


WORKFLOW_ID = "fuzzy-name-count"


# ─── cascade tier handlers (each is exported for reuse) ──────────────

def load_transcripts_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    transcripts_dir = ctx.inputs.get("transcripts_dir")
    if not transcripts_dir:
        return CascadeStep(kind="escalate", reason="missing_input:transcripts_dir")
    try:
        transcripts = _lib_load_transcripts(Path(transcripts_dir))
    except FileNotFoundError as e:
        return CascadeStep(kind="escalate", reason=f"transcripts_load_failed:{e}")
    if not transcripts:
        return CascadeStep(kind="no_op", reason="no_transcripts_found")
    return CascadeStep(
        kind="continue", reason=f"{len(transcripts)} transcript(s) loaded",
        metadata={"transcripts": transcripts},
    )


def build_aliases_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    names = ctx.inputs.get("names") or []
    if not names:
        return CascadeStep(kind="escalate", reason="missing_input:names (list of full names)")
    aliases = [name_aliases(n) for n in names if name_aliases(n)]
    if not aliases:
        return CascadeStep(kind="no_op", reason="no_usable_aliases_built")
    return CascadeStep(
        kind="continue", reason=f"built aliases for {len(aliases)} name(s)",
        metadata={"aliases": aliases, "names_used": [n for n in names if name_aliases(n)]},
    )


def identify_target_speaker_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Documents the per-transcript speaker-identification rule.
    The actual top-speaker pick happens inside count_callouts. This tier
    just declares the policy + records it in the audit trail.
    """
    any_speaker = ctx.inputs.get("any_speaker", False)
    return CascadeStep(
        kind="continue",
        reason="any_speaker_mode" if any_speaker else "top_speaker_mode",
        metadata={"any_speaker": any_speaker},
    )


def count_callouts_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    transcripts = ctx.accumulated.get("transcripts") or []
    aliases = ctx.accumulated.get("aliases") or []
    if not transcripts or not aliases:
        return CascadeStep(kind="escalate", reason="precondition_failed:missing transcripts or aliases")
    threshold = ctx.inputs.get("threshold", cfg.get("base_threshold", 85))
    any_speaker = ctx.accumulated.get("any_speaker", False)
    per_session = [count_callouts(t, aliases, threshold, any_speaker) for t in transcripts]
    headers = [t.get("start_time", "")[:10] or t.get("meeting_id", "")[:10] for t in transcripts]
    return CascadeStep(
        kind="continue",
        reason=f"counted {sum(sum(s) for s in per_session)} total callouts",
        metadata={"per_session_counts": per_session, "session_headers": headers},
    )


def emit_csv_handler(ctx: CascadeContext, cfg: dict[str, Any]) -> CascadeStep:
    """Final tier — write CSV, return ready_to_propose with the matrix."""
    output_csv = ctx.inputs.get("output_csv")
    names_used = ctx.accumulated.get("names_used") or []
    per_session = ctx.accumulated.get("per_session_counts") or []
    headers = ctx.accumulated.get("session_headers") or []

    rows_out = []
    for i, name in enumerate(names_used):
        counts = [per_session[s][i] for s in range(len(per_session))]
        rows_out.append([name] + counts + [sum(counts)])

    if output_csv:
        p = Path(output_csv)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Name"] + headers + ["Total"])
            for row in rows_out:
                w.writerow(row)

    return CascadeStep(
        kind="ready_to_propose",
        reason=f"emitted {len(rows_out)} rows",
        metadata={"rows": rows_out, "headers": headers + ["Total"], "csv_path": output_csv},
    )


# Register at import time
register_rules_handler(WORKFLOW_ID, "load_transcripts", load_transcripts_handler)
register_rules_handler(WORKFLOW_ID, "build_aliases", build_aliases_handler)
register_rules_handler(WORKFLOW_ID, "identify_target_speaker", identify_target_speaker_handler)
register_rules_handler(WORKFLOW_ID, "count_callouts", count_callouts_handler)
register_rules_handler(WORKFLOW_ID, "emit_csv", emit_csv_handler)


# ─── public entry point ─────────────────────────────────────────────

def run(inputs: dict[str, Any]) -> CascadeResult:
    manifest, report = load_skill_manifest(Path(__file__).parent)
    if not report.ok:
        raise RuntimeError(f"manifest invalid: {report.summary()}")
    return run_cascade(manifest=manifest, inputs=inputs)


# ─── eval helpers ────────────────────────────────────────────────────

def run_canary(case: dict) -> dict:
    """Fast eval entry point — uses count_callouts directly without the cascade."""
    inp = case["input"]
    segments = inp["transcript_segments"]
    names = inp["names"]
    aliases = [name_aliases(n) for n in names if name_aliases(n)]
    transcript = {"meeting_id": "fixture", "title": "fixture",
                  "start_time": "2026-01-01T00:00:00Z", "transcript": segments}
    counts = count_callouts(
        transcript, aliases,
        threshold=inp.get("threshold", 85),
        any_speaker=inp.get("any_speaker", False),
    )
    return {"counts": counts}


# ─── CLI ────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="fuzzy-name-count (SAI atomic skill).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run", help="Run the cascade end-to-end.")
    sp.add_argument("--transcripts", required=True, type=Path)
    sp.add_argument("--names-file", required=True, type=Path)
    sp.add_argument("--output-csv", required=True, type=Path)
    sp.add_argument("--threshold", type=int, default=85)
    sp.add_argument("--any-speaker", action="store_true")
    args = p.parse_args()

    if args.cmd == "run":
        names = [ln.strip() for ln in args.names_file.read_text().splitlines() if ln.strip()]
        result = run({
            "transcripts_dir": str(args.transcripts),
            "names": names,
            "output_csv": str(args.output_csv),
            "threshold": args.threshold,
            "any_speaker": args.any_speaker,
        })
        print(json.dumps({
            "final_verdict": result.final_verdict,
            "final_reason": result.final_reason,
            "audit_log": result.audit_log,
        }, indent=2, default=str))
        return 0 if result.final_verdict == "ready_to_propose" else 1


if __name__ == "__main__":
    sys.exit(main())
