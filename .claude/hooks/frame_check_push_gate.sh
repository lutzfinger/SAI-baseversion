#!/usr/bin/env bash
# frame_check_push_gate.sh - Claude Code PreToolUse (Bash) hook.
#
# Blocks `git push` of CODE-shaped changes when there is NO fresh Step 0
# Frame-Check marker for the repo. Doc/config/eval-only pushes, an explicit
# FRAME_CHECK_OVERRIDE=1, or a fresh marker -> ALLOWED. These are the TEETH that
# frame_check_nudge.sh (soft) lacks; both read the same marker.
#
# THREAT MODEL (same posture as danger_guard.sh): this is a backstop against
# FORGETTING/SKIPPING the frame, not an adversary-proof gate. It FAILS OPEN on any
# ambiguity (no upstream, detached HEAD, unparseable command, missing python3/git,
# no detectable ahead-diff) - the failure mode is "no gate", never "bricked push".
# It only ever blocks on a positive "code ahead of upstream + no fresh marker" match.
set -uo pipefail

# Read payload from stdin into a var; pass to python via env (see nudge note on
# why `python3 - <<PY` would eat the JSON).
FRAME_PAYLOAD="$(cat)"

# N6c: an override is a deliberate bypass — record it to the CANONICAL audit sink
# (app/canonical/action_log.record_action, the shared primitive; NOT a parallel
# file — #4/#14). Best-effort: never blocks; if the runtime venv is missing, say
# so loudly on stderr rather than logging silently to a side file.
if [ "${FRAME_CHECK_OVERRIDE:-}" = "1" ]; then
    # BASE (the base repo with the venv + audit primitive) is operator-specific,
    # so it comes from the env — never hardcoded in this PUBLIC hook (#17). If
    # unset/missing, degrade loudly rather than logging silently.
    BASE="${SAI_BASEVERSION:-}"
    if [ -n "$BASE" ] && [ -x "$BASE/.venv/bin/python" ]; then
        FRAME_PAYLOAD="$FRAME_PAYLOAD" SAI_BASE="$BASE" "$BASE/.venv/bin/python" <<'PY' 2>/dev/null || true
import os, sys, json
sys.path.insert(0, os.environ.get("SAI_BASE", ""))
try:
    from app.canonical.action_log import record_action
    d = json.loads(os.environ.get("FRAME_PAYLOAD") or "{}")
    cwd = d.get("cwd") or os.getcwd()
    record_action(
        "frame_check.override",
        target_id=cwd,
        summary="frame-check push gate bypassed via FRAME_CHECK_OVERRIDE=1",
        skill_id="plan-test-code",
        payload={"hook": "frame_check_push_gate"},
    )
except Exception:
    pass
PY
    else
        echo "frame_check_push_gate: OVERRIDE used but audit venv missing at $BASE/.venv — bypass NOT logged" >&2
    fi
    exit 0
fi
DECISION="$(FRAME_PAYLOAD="$FRAME_PAYLOAD" python3 <<'PY' 2>/dev/null
import sys, json, os, re, hashlib, subprocess, time
try:
    d = json.loads(os.environ.get("FRAME_PAYLOAD") or "{}")
    cmd = d.get("tool_input", {}).get("command", "") or ""
    cwd = d.get("cwd") or os.getcwd()
except Exception:
    sys.exit(0)  # fail open
if not isinstance(cmd, str) or not re.search(r"(?:^|[^A-Za-z0-9_])(?:[\w./-]*/)?git\b[^;&|]*\bpush\b", cmd):
    sys.exit(0)  # not a git push -> allow

def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout.strip()

try:
    repo = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"]) or cwd
    files = run(["git", "-C", repo, "diff", "--name-only", "@{upstream}..HEAD"])
except Exception:
    sys.exit(0)  # fail open (no upstream / detached / no git)
if not files:
    sys.exit(0)  # nothing detectable ahead -> fail open

paths = [f for f in files.splitlines() if f.strip()]
DOC = re.compile(r"(\.md$|\.txt$|\.rst$|^docs/|LOOSE-ENDS|README|\.jsonl$)", re.I)
# N6a: a SKILL is code even when its changed files are .md/.yaml (SKILL.md,
# skill.yaml). The old doc-bypass let a SKILL.md-only push ship ungated. Any
# path under skills/ is treated as code, never doc.
SKILL = re.compile(r"(^|/)skills/", re.I)
code_paths = [p for p in paths if SKILL.search(p) or not DOC.search(p)]
if not code_paths:
    sys.exit(0)  # doc/config/eval-only -> allow

TTL = float(os.environ.get("FRAME_CHECK_TTL_HOURS", "24")) * 3600
key = hashlib.sha1(repo.encode()).hexdigest()
marker = os.path.expanduser("~/.claude/frame-check/%s.json" % key)
if os.path.exists(marker) and (time.time() - os.path.getmtime(marker)) < TTL:
    # N6b (WARN-first, no lockout): a fresh marker allows the push, but if it
    # carries no review-evidence (older timestamp-only marker), emit a soft
    # signal so the model self-certifying an empty frame is at least visible.
    # Promoted to a hard requirement only after burn-in.
    try:
        m = json.loads(open(marker).read() or "{}")
    except Exception:
        m = {}
    if not m.get("review_sha"):
        print("WARN_NOEV")
    sys.exit(0)  # frame confirmed -> allow
print("BLOCK")
PY
)"

if [ "$DECISION" = "BLOCK" ]; then
    echo "frame_check_push_gate: BLOCKED - pushing code with no confirmed Step 0 Frame-Check for this repo." >&2
    echo "Run plan-test-code (Step 0 Frame-Check), or override: FRAME_CHECK_OVERRIDE=1 git push ..." >&2
    exit 2
fi
if [ "$DECISION" = "WARN_NOEV" ]; then
    # N6b burn-in: allow, but flag that the frame marker carries no review evidence.
    echo "frame_check_push_gate: NOTE - frame marker has no review evidence (review_sha); allowed during burn-in." >&2
fi
exit 0
