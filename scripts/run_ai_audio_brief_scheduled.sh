#!/bin/sh

set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
LOG_DIR="${SAI_LOG_DIR:-$HOME/Library/Logs/SAI}/scheduled"
STATE_DIR="$LOG_DIR/state"
LOCK_DIR="$LOG_DIR/ai_audio_brief.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
STATE_FILE="$STATE_DIR/ai_audio_brief.json"
LOG_FILE="$LOG_DIR/ai_audio_brief.log"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

mkdir -p "$LOG_DIR" "$STATE_DIR"
exec >>"$LOG_FILE" 2>&1

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

printf '[%s] ai-audio-brief scheduler start\n' "$(timestamp)"

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_PID_FILE"
    return 0
  fi

  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid=$(cat "$LOCK_PID_FILE" 2>/dev/null || true)
    if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
      printf '[%s] skip: previous ai-audio-brief run is still active (pid=%s)\n' "$(timestamp)" "$lock_pid"
      exit 0
    fi
    printf '[%s] stale lock detected for ai-audio-brief run (pid=%s); clearing lock\n' "$(timestamp)" "${lock_pid:-unknown}"
    rm -f "$LOCK_PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  else
    printf '[%s] stale lock detected for ai-audio-brief run (no pid); clearing lock\n' "$(timestamp)"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_PID_FILE"
    return 0
  fi

  printf '[%s] skip: could not acquire ai-audio-brief lock\n' "$(timestamp)"
  exit 0
}

acquire_lock

cleanup() {
  rm -f "$LOCK_PID_FILE" >/dev/null 2>&1 || true
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

slot_key=
if slot_key=$("$PYTHON_BIN" "$SCRIPT_DIR/scheduler_gate.py" due --state-file "$STATE_FILE" --slot 07:00); then
  printf '[%s] gate: due slot=%s\n' "$(timestamp)" "$slot_key"
else
  status=$?
  if [ "$status" -eq 10 ]; then
    printf '[%s] skip: ai-audio-brief is not due yet\n' "$(timestamp)"
    exit 0
  fi
  printf '[%s] ai-audio-brief gate failed exit=%s\n' "$(timestamp)" "$status"
  exit "$status"
fi

printf '[%s] run: ai audio brief\n' "$(timestamp)"
cd "$REPO_ROOT"
printf '[%s] mode: runtime env + keychain refs (fail closed)\n' "$(timestamp)"
SAI_RUNTIME_REQUIRED_VARS='OPENAI_API_KEY,SAI_LANGSMITH_API_KEY,SAI_SLACK_BOT_TOKEN' \
"$SCRIPT_DIR/with_runtime_env.sh" \
  "$PYTHON_BIN" \
  "$REPO_ROOT/scripts/run_email_triage.py" \
  ai-audio-brief-daily

status=$?
if [ "$status" -eq 0 ]; then
  "$PYTHON_BIN" "$SCRIPT_DIR/scheduler_gate.py" mark --state-file "$STATE_FILE" --slot-key "$slot_key"
fi
printf '[%s] ai-audio-brief scheduler exit=%s\n' "$(timestamp)" "$status"
exit "$status"
