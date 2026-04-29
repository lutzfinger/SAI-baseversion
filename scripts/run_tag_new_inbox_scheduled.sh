#!/bin/sh

set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
LOG_DIR="${SAI_LOG_DIR:-$HOME/Library/Logs/SAI}/scheduled"
LOCK_DIR="$LOG_DIR/tag_new_inbox.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
LOG_FILE="$LOG_DIR/tag_new_inbox.log"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

printf '[%s] scheduled tagger start\n' "$(timestamp)"

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_PID_FILE"
    return 0
  fi

  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid=$(cat "$LOCK_PID_FILE" 2>/dev/null || true)
    if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
      printf '[%s] skip: previous tagging run is still active (pid=%s)\n' "$(timestamp)" "$lock_pid"
      exit 0
    fi
    printf '[%s] stale lock detected for tagging run (pid=%s); clearing lock\n' "$(timestamp)" "${lock_pid:-unknown}"
    rm -f "$LOCK_PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  else
    printf '[%s] stale lock detected for tagging run (no pid); clearing lock\n' "$(timestamp)"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_PID_FILE"
    return 0
  fi

  printf '[%s] skip: could not acquire tagging lock\n' "$(timestamp)"
  exit 0
}

acquire_lock

cleanup() {
  rm -f "$LOCK_PID_FILE" >/dev/null 2>&1 || true
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"$SCRIPT_DIR/ensure_ollama.sh" >/dev/null 2>&1 || true

printf '[%s] run: tagging inbox threads, including ones with third-party user labels\n' "$(timestamp)"
cd "$REPO_ROOT"
printf '[%s] mode: runtime env + keychain refs (fail closed)\n' "$(timestamp)"
SAI_RUNTIME_REQUIRED_VARS='OPENAI_API_KEY,SAI_LANGSMITH_API_KEY,SAI_SLACK_BOT_TOKEN' \
"$SCRIPT_DIR/with_runtime_env.sh" \
  "$PYTHON_BIN" \
  "$REPO_ROOT/scripts/classify_and_tag_gmail.py" \
  --workflow email-triage-gmail-tagging \
  --max-results 500 \
  --query "in:inbox" \
  --require-cloud \
  --require-langsmith

status=$?
printf '[%s] scheduled tagger exit=%s\n' "$(timestamp)" "$status"
exit "$status"
