#!/usr/bin/env bash
# frame_check_nudge.sh - Claude Code UserPromptSubmit hook.
#
# On a coding-shaped prompt with NO fresh Step 0 Frame-Check marker for the repo,
# injects a reminder to run plan-test-code Step 0 (expose framing -> different-vendor
# review -> operator confirms the frame). Once the frame is confirmed (marker present
# within TTL) it stays SILENT, so it fires at most once per frame - no wallpaper.
#
# Soft by design: it only injects context, never blocks. Fails OPEN (no injection)
# on ANY error - a broken nudge must never disrupt a prompt. Companion teeth live in
# frame_check_push_gate.sh. Marker written by plan-test-code's frame_confirmed.sh.
set -uo pipefail

# Read the hook payload from stdin into a var, then hand it to python via env.
# (Do NOT use `python3 - <<PY`: that feeds the heredoc program on stdin, so the
# real JSON never reaches the script. danger_guard sidesteps this with `-c`.)
FRAME_PAYLOAD="$(cat)"
OUT="$(FRAME_PAYLOAD="$FRAME_PAYLOAD" python3 <<'PY' 2>/dev/null
import sys, json, os, re, hashlib, subprocess, time
try:
    d = json.loads(os.environ.get("FRAME_PAYLOAD") or "{}")
except Exception:
    sys.exit(0)
prompt = d.get("prompt", "") or ""
cwd = d.get("cwd") or os.getcwd()
if not isinstance(prompt, str):
    sys.exit(0)

# coding-shaped intent: a strong standalone signal, OR a build-verb near a code-noun.
CODE = re.compile(
    r"\b(implement|refactor|debug)\b"
    r"|\b(build|add|fix|create|write|change|update|wire|patch|design|extend|introduce)\b"
    r"[^.?!]{0,60}\b(code|feature|skill|hook|function|script|worker|workflow|module|"
    r"class|endpoint|api|test|bug|method|handler|component|schema|migration|config|"
    r"parser|client|service|daemon|gate|pipeline|connector)\b",
    re.I,
)
if not CODE.search(prompt):
    sys.exit(0)

TTL = float(os.environ.get("FRAME_CHECK_TTL_HOURS", "24")) * 3600
try:
    repo = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=5).stdout.strip() or cwd
except Exception:
    repo = cwd
key = hashlib.sha1(repo.encode()).hexdigest()
marker = os.path.expanduser("~/.claude/frame-check/%s.json" % key)
if os.path.exists(marker) and (time.time() - os.path.getmtime(marker)) < TTL:
    sys.exit(0)  # frame already confirmed for this repo -> stay silent

msg = ("Frame-Check reminder: this looks like build/architecture work. Before planning "
       "or editing code, run plan-test-code Step 0 Frame-Check - expose your framing, run "
       "the different-vendor review, and get the operator to confirm the frame. "
       "(Set FRAME_CHECK_TTL_HOURS or ignore if this is trivial.)")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit", "additionalContext": msg}}))
PY
)"
[ -n "$OUT" ] && printf '%s\n' "$OUT"
exit 0
