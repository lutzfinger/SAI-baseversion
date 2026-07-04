#!/usr/bin/env bash
# loop_example.sh - drive the bounded loop with the headless Claude Code agent.
#
# A TEMPLATE. Set TASK and ESCAPE_CMD for your job, then run it FROM A PLAIN
# TERMINAL (or later, cron). Do NOT run it from inside a live Claude Code session
# - it would nest `claude`.
#
# Safety: it runs in a throwaway git worktree (a concurrent human writer is not
# disturbed) and it NEVER pushes - the runner does not push, the agent is told
# "edit only", and branch protection is the backstop. `--permission-mode
# acceptEdits` auto-approves edits while KEEPING the danger_guard/boundary_guard
# PreToolUse hooks blocking (they fire before permissions). NEVER use
# --dangerously-skip-permissions here.
#
# Caveat: the agent may need to run its own bash (run tests, commit progress);
# acceptEdits auto-approves edits but bash tools may need an allowedTools config -
# confirm on your first live run. Instruct the agent to commit each iteration so
# progress persists on the branch after the worktree is removed.
set -euo pipefail

TASK="${TASK:-Make the failing tests pass. Edit files only; commit each step; do not push or commit to main.}"
ESCAPE_CMD="${ESCAPE_CMD:-.venv/bin/python -m pytest -q}"
MAX_ITERATIONS="${MAX_ITERATIONS:-8}"
MAX_SECONDS="${MAX_SECONDS:-1800}"

ROOT="$(git rev-parse --show-toplevel)"
WORKTREE="$(mktemp -d)/loop-worktree"
BRANCH="loop/$(date +%s)"

git -C "$ROOT" worktree add -q -b "$BRANCH" "$WORKTREE"
cleanup() { git -C "$ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$ROOT/scripts/bounded_loop.py" \
    --repo "$WORKTREE" \
    --attempt-cmd "cd '$WORKTREE' && claude -p '$TASK' --permission-mode acceptEdits" \
    --escape-cmd "cd '$WORKTREE' && $ESCAPE_CMD" \
    --max-iterations "$MAX_ITERATIONS" \
    --max-seconds "$MAX_SECONDS"

echo "Loop finished (exit $?). Review the diff on branch $BRANCH before you decide to push."
