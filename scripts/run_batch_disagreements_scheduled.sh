#!/bin/sh
# Hourly trigger for Loop 2 v1 — surface a disagreement batch ask
# when eval/disagreement_queue.jsonl crosses DISAGREEMENT_BATCH_THRESHOLD.
#
# No-ops below threshold. Triggered by launchd plist
# `com.sai.batch-disagreements.plist` on a 3600-second interval.

set -eu
umask 077

# Run from the merged-runtime tree (SAI-baseversion + SAI overlay).
# This is where both the public CLI (sai_eval) and the private rules /
# overlay live merged. Override with SAI_RUNTIME_DIR if moved.
REPO_ROOT="${SAI_RUNTIME_DIR:-$HOME/.sai-runtime}"
LOG_DIR="${SAI_LOG_DIR:-$HOME/Library/Logs/SAI}/scheduled"
LOCK_DIR="$LOG_DIR/batch_disagreements.lock"

mkdir -p "$LOG_DIR"

# Single-instance lock to avoid overlap if a run takes >1h.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) batch_disagreements: lock held; skipping" >> "$LOG_DIR/batch_disagreements.log"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

PY="${SAI_PYTHON:-$REPO_ROOT/.venv/bin/python}"
cd "$REPO_ROOT"

LOG="$LOG_DIR/batch_disagreements.log"
echo "$(date -u +%FT%TZ) batch_disagreements: start" >> "$LOG"
"$PY" -m scripts.sai_eval batch-disagreements >> "$LOG" 2>&1
echo "$(date -u +%FT%TZ) batch_disagreements: end" >> "$LOG"
