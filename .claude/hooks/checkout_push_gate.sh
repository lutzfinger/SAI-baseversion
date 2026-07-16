#!/usr/bin/env bash
# checkout_push_gate.sh - Claude Code PreToolUse (Bash) hook.
#
# INVOKES the code-checkout preflight at `git push` time for any SAI skill
# changed in the push. This closes the structural gap (RC3): the preflight's
# "13-check hard gate" existed but NOTHING ran it — it was prose the model
# chose to honor. This hook makes it fire.
#
# POSTURE — WARN-FIRST (operator decision 2026-07-15): by default it PRINTS
# preflight FAILs as warnings and ALLOWS the push (burn-in). Set
# CHECKOUT_PUSH_GATE_BLOCK=1 to promote it to a hard block (exit 2) once it has
# proven quiet. Like danger_guard/frame_check_push_gate it FAILS OPEN on any
# ambiguity (no upstream, non-SAI repo, missing preflight) — never bricks a push.
#
# Scope: only acts on pushes from the SAI working repo or SAI-baseversion; every
# other project's git push is untouched.
set -uo pipefail

FRAME_PAYLOAD="$(cat)"
DImap="$(FRAME_PAYLOAD="$FRAME_PAYLOAD" python3 <<'PY' 2>/dev/null
import sys, json, os, re, subprocess
try:
    d = json.loads(os.environ.get("FRAME_PAYLOAD") or "{}")
    cmd = d.get("tool_input", {}).get("command", "") or ""
    cwd = d.get("cwd") or os.getcwd()
except Exception:
    sys.exit(0)  # fail open
if not isinstance(cmd, str) or not re.search(r"(?:^|[^A-Za-z0-9_])(?:[\w./-]*/)?git\b[^;&|]*\bpush\b", cmd):
    sys.exit(0)  # not a git push

def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout.strip()

try:
    repo = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"]) or cwd
except Exception:
    sys.exit(0)
# Only act on the SAI repos (by basename); leave every other project alone.
if os.path.basename(repo) not in ("SAI", "SAI-baseversion"):
    sys.exit(0)
try:
    files = run(["git", "-C", repo, "diff", "--name-only", "@{upstream}..HEAD"])
except Exception:
    sys.exit(0)  # no upstream -> fail open
if not files:
    sys.exit(0)
skills = sorted({m.group(1) for f in files.splitlines()
                 for m in [re.search(r"(?:^|/)skills/([^/]+)/", f)] if m})
if not skills:
    sys.exit(0)  # no skill changed -> nothing to preflight
print(repo + "\t" + ",".join(skills))
PY
)"

[ -z "$DImap" ] && exit 0
REPO="${DImap%%$'\t'*}"
SKILLS_CSV="${DImap#*$'\t'}"

# Resolve the preflight from generic/runtime-derived locations only — no operator
# path hardcoded in this PUBLIC hook (#17). SAI_REPO env is an optional override.
PREFLIGHT=""
for c in "$HOME/.claude/skills/code-checkout/scripts/checkout_preflight.sh" \
         "$REPO/skills/code-checkout/scripts/checkout_preflight.sh" \
         "${SAI_REPO:-}/skills/code-checkout/scripts/checkout_preflight.sh"; do
  [ -n "$c" ] && [ -x "$c" ] && { PREFLIGHT="$c"; break; }
done
[ -z "$PREFLIGHT" ] && exit 0  # fail open — can't find the preflight

ARGS=(--repo "$REPO" --pre-deploy --skip-suite)
IFS=',' read -r -a SKILL_ARR <<< "$SKILLS_CSV"
for s in "${SKILL_ARR[@]}"; do ARGS+=(--skill "$s"); done

OUT="$(bash "$PREFLIGHT" "${ARGS[@]}" 2>&1)"; RC=$?
FAILS="$(printf '%s\n' "$OUT" | grep -E '^FAIL ' || true)"

if [ -n "$FAILS" ]; then
  echo "checkout_push_gate: preflight FAIL rows for changed skill(s) [$SKILLS_CSV]:" >&2
  printf '%s\n' "$FAILS" | sed 's/^/  /' >&2
  if [ "${CHECKOUT_PUSH_GATE_BLOCK:-}" = "1" ]; then
    echo "checkout_push_gate: BLOCKED (CHECKOUT_PUSH_GATE_BLOCK=1). Fix the rows or override with CHECKOUT_PUSH_GATE_BLOCK=0." >&2
    exit 2
  fi
  echo "checkout_push_gate: WARN-only (burn-in). Set CHECKOUT_PUSH_GATE_BLOCK=1 to enforce." >&2
fi
exit 0
