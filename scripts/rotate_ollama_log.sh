#!/bin/sh
# Rotate ~/Library/Logs/SAI/ollama-auto.log when it exceeds a size cap.
# Truncate-in-place to avoid disturbing the running ollama process's open fd.
# Idempotent. Designed for nightly cron / launchd.

set -eu

LOG_PATH="${SAI_OLLAMA_LOG_PATH:-$HOME/Library/Logs/SAI/ollama-auto.log}"
MAX_MB="${SAI_OLLAMA_LOG_MAX_MB:-50}"
KEEP_TAIL_LINES="${SAI_OLLAMA_LOG_KEEP_TAIL_LINES:-2000}"

if [ ! -f "$LOG_PATH" ]; then
  echo "ollama-auto.log not found at $LOG_PATH; nothing to rotate"
  exit 0
fi

size_bytes=$(stat -f "%z" "$LOG_PATH" 2>/dev/null || stat -c "%s" "$LOG_PATH")
size_mb=$(( size_bytes / 1024 / 1024 ))

echo "ollama log: $LOG_PATH ($size_mb MB / $MAX_MB MB cap)"

if [ "$size_mb" -lt "$MAX_MB" ]; then
  echo "  under cap — no rotation"
  exit 0
fi

# Keep the last N lines; truncate the file in place. The ollama process keeps
# writing through its open fd; truncate-in-place leaves the fd valid (writes
# continue after the new offset 0).
TMP_TAIL="$(mktemp)"
tail -n "$KEEP_TAIL_LINES" "$LOG_PATH" >"$TMP_TAIL"
: >"$LOG_PATH"
cat "$TMP_TAIL" >>"$LOG_PATH"
rm -f "$TMP_TAIL"

new_bytes=$(stat -f "%z" "$LOG_PATH" 2>/dev/null || stat -c "%s" "$LOG_PATH")
new_mb=$(( new_bytes / 1024 / 1024 ))
echo "  rotated: kept last $KEEP_TAIL_LINES lines (now ${new_mb} MB)"
