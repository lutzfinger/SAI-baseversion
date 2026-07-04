#!/usr/bin/env bash
# danger_guard.sh - Claude Code PreToolUse (Bash) hook.
#
# Blocks a small set of destructive commands an agent should never run
# unprompted: `git reset --hard`, `git push --force` / `-f`, and `sudo`.
# Pattern harvested from yoshpy-dev/ralph's pre_bash_guard.sh (MIT), reimplemented
# in this repo's style (python3 word-boundary parse + exit-2 block, matching
# boundary_guard.sh; Ralph's shared sed fallback is fragile by their own note).
#
# THREAT MODEL: this is a DENYLIST of known-bad commands, not a boundary. It
# catches the ACCIDENTAL / common case and adds friction. It is best-effort and
# NOT adversary-proof: an obfuscated form (`git${IFS}reset --hard`, an alias, or a
# missing python3) slips through. For the public repo the real safety net is CI +
# branch protection; for destructive local ops there is no server-side net, so
# treat this as friction, not a guarantee. Because it is a denylist, it FAILS OPEN
# on uncertainty by design: a payload it cannot parse, or a command it does not
# recognize, is ALLOWED (exit 0). It only ever blocks a positive match.
#
# `--force-with-lease` (the safe force-push variant) is intentionally allowed.
set -uo pipefail

REASON="$(python3 -c '
import sys, json, re
try:
    c = json.load(sys.stdin).get("tool_input", {}).get("command")
except Exception:
    c = ""
if not isinstance(c, str):
    c = ""
GIT = r"(?:^|[^A-Za-z0-9_])(?:[\w./-]*/)?git\s+(?:\S+\s+)*?"
PATTERNS = [
    ("git reset --hard", re.compile(GIT + r"reset\b[^;&|]*--hard\b")),
    ("git push --force / -f", re.compile(GIT + r"push\b[^;&|]*(?:--force(?![\w-])|(?:^|\s)-f(?:\s|$))")),
    ("sudo", re.compile(r"(?:^|[^A-Za-z0-9_])sudo(?:\s|$)")),
]
for name, pat in PATTERNS:
    if pat.search(c):
        print(name)
        break
' 2>/dev/null)"

if [ -n "$REASON" ]; then
    echo "danger_guard: BLOCKED - '$REASON' is destructive and disabled in this harness." >&2
    echo "If you truly need it, run it yourself outside the agent." >&2
    exit 2
fi
exit 0
