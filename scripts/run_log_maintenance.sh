#!/bin/sh
# Nightly log maintenance: rotate audit.jsonl, prune langgraph checkpoints,
# rotate ollama-auto.log. Idempotent. Each step is independent — a failure
# in one does not stop the others.

set -u  # not -e: keep going on per-step failure

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN="${SAI_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"

LOG_DIR="${SAI_LOG_DIR:-$HOME/Library/Logs/SAI}"
MAINT_LOG="$LOG_DIR/log-maintenance.log"
mkdir -p "$LOG_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

{
  printf '\n[%s] === log maintenance start ===\n' "$(ts)"

  printf '[%s] step 1/3: rotate audit log\n' "$(ts)"
  "$PYTHON_BIN" "$SCRIPT_DIR/rotate_audit_log.py" || \
    printf '[%s]   audit rotation FAILED\n' "$(ts)"

  printf '[%s] step 2/3: prune langgraph checkpoints\n' "$(ts)"
  "$PYTHON_BIN" "$SCRIPT_DIR/prune_langgraph_checkpoints.py" || \
    printf '[%s]   checkpoint prune FAILED\n' "$(ts)"

  printf '[%s] step 3/3: rotate ollama log\n' "$(ts)"
  sh "$SCRIPT_DIR/rotate_ollama_log.sh" || \
    printf '[%s]   ollama rotation FAILED\n' "$(ts)"

  printf '[%s] === log maintenance done ===\n' "$(ts)"
} >>"$MAINT_LOG" 2>&1
