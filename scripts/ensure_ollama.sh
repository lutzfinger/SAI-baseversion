#!/bin/sh

# Ensure the local Ollama server is running. If it is not, start it in the
# background and wait until the HTTP API is reachable.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

AUTO_START=${SAI_AUTO_START_OLLAMA:-true}
case "$(printf '%s' "$AUTO_START" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off)
    exit 0
    ;;
esac

LOCAL_ENABLED=${SAI_LOCAL_LLM_ENABLED:-true}
case "$(printf '%s' "$LOCAL_ENABLED" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off)
    exit 0
    ;;
esac

OLLAMA_BIN=${OLLAMA_BIN:-/opt/homebrew/bin/ollama}
if [ ! -x "$OLLAMA_BIN" ]; then
  if command -v ollama >/dev/null 2>&1; then
    OLLAMA_BIN=$(command -v ollama)
  else
    echo "Ollama is not installed or not on PATH, so SAI cannot auto-start it." >&2
    exit 1
  fi
fi

OLLAMA_URL=${SAI_LOCAL_LLM_HOST:-${OLLAMA_API_URL:-http://127.0.0.1:11434}}
OLLAMA_BIND=${OLLAMA_HOST:-$(printf '%s' "$OLLAMA_URL" | sed 's#^https\?://##')}
START_TIMEOUT=${SAI_OLLAMA_START_TIMEOUT_SECONDS:-30}
OLLAMA_LOG_PATH=${SAI_OLLAMA_LOG_PATH:-"$HOME/Library/Logs/SAI/ollama-auto.log"}
OLLAMA_PID_PATH=${SAI_OLLAMA_PID_PATH:-"$HOME/Library/Logs/SAI/ollama-auto.pid"}

mkdir -p "$(dirname "$OLLAMA_LOG_PATH")"

is_ready() {
  curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1
}

if is_ready; then
  exit 0
fi

if [ -f "$OLLAMA_PID_PATH" ]; then
  EXISTING_PID=$(cat "$OLLAMA_PID_PATH" 2>/dev/null || true)
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
    :
  else
    rm -f "$OLLAMA_PID_PATH"
  fi
fi

if [ ! -f "$OLLAMA_PID_PATH" ]; then
  echo "Starting Ollama in the background for SAI..." >&2
  (
    export OLLAMA_HOST="$OLLAMA_BIND"
    nohup "$OLLAMA_BIN" serve >>"$OLLAMA_LOG_PATH" 2>&1 &
    echo $! >"$OLLAMA_PID_PATH"
  )
fi

SECONDS_WAITED=0
while [ "$SECONDS_WAITED" -lt "$START_TIMEOUT" ]; do
  if is_ready; then
    exit 0
  fi
  sleep 1
  SECONDS_WAITED=$((SECONDS_WAITED + 1))
done

echo "Ollama did not become ready within ${START_TIMEOUT}s." >&2
echo "Check the log at $OLLAMA_LOG_PATH" >&2
exit 1
