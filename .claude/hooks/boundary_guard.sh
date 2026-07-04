#!/usr/bin/env bash
# boundary_guard.sh - Claude Code PreToolUse (Bash) hook.
#
# Blocks a git command that records history or contacts a remote when the
# boundary linter finds private data, so an agent working in this repo cannot
# ship personal data even if the git pre-commit hook was never installed.
# Layer 3 of the boundary guarantee (see docs/ralph-extension.md).
#
# THREAT MODEL: this layer catches the ACCIDENTAL case (an agent about to commit
# private data without realizing) and adds friction. It is best-effort matching
# and is NOT a defense against a determined adversary, who can obfuscate a command
# (for example `git${IFS}commit`) or rewrite the linter. That adversary is stopped
# by Layer 1: CI + branch protection require the boundary check to pass before
# anything merges to the public main branch. Do not treat this hook as the guarantee.
#
# Contract (verified): the tool payload arrives on stdin as JSON; the bash command
# is at .tool_input.command. Exit 2 blocks the tool call; exit 0 allows.
#
# Fail-closed: a payload with no string command, a missing linter, or a linter
# that exits non-zero (violation OR crash) all block with exit 2.
set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Decide whether the command is a history/remote-writing git command. Detection
# runs in python3 (guaranteed present; no jq) with word-boundary matching so
# innocent strings like "digit committee" do not match. Fail closed: no readable
# string command -> non-zero exit -> block.
DECISION="$(python3 -c '
import sys, json, re
try:
    c = json.load(sys.stdin).get("tool_input", {}).get("command")
except Exception:
    sys.exit(3)
if not isinstance(c, str) or not c.strip():
    sys.exit(4)
# git as a command token: optional path prefix (/usr/bin/git), optional global
# options and their values (git -c k=v commit), then a history/remote-writing
# subcommand. Left word-boundary avoids false matches like "digit committee".
# Shell obfuscation (git${IFS}commit) and indirect invocation (sh -c, make) are
# out of scope by design; Layer 1 (CI + branch protection) is the guarantee.
pat = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:[\w./-]*/)?git\s+(?:\S+\s+)*?"
    r"(commit|push|merge|rebase|cherry-pick|am|commit-tree|send-pack|update-ref|fast-import)"
    r"(?:\s|$)"
)
print("GUARD" if pat.search(c) else "PASS")
' 2>/dev/null)"
if [ $? -ne 0 ]; then
    echo "boundary_guard: no readable shell command in the Bash payload - blocking (fail-closed)." >&2
    exit 2
fi

case "$DECISION" in
    GUARD) ;;
    PASS) exit 0 ;;
    *) echo "boundary_guard: unexpected decision '$DECISION' - blocking (fail-closed)." >&2; exit 2 ;;
esac

LINTER="$PROJECT_DIR/scripts/boundary_check.py"
if out="$(python3 -- "$LINTER" 2>&1)"; then
    exit 0
fi

echo "boundary_guard: BLOCKED - the boundary linter reported a problem:" >&2
echo "$out" >&2
echo "Fix the flagged term, or add the path to boundary_check_allowlist.txt if it is a false positive, then retry." >&2
exit 2
