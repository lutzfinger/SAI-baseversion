#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON:-"$REPO_ROOT/.venv/bin/python"}
HEARTBEAT_PATH=${SAI_SLACK_SOCKET_HEARTBEAT_PATH:-"$HOME/Library/Application Support/SAI/state/services/slack_socket_mode.heartbeat"}

export PYTHONUNBUFFERED=1
export SAI_RUNTIME_REQUIRED_VARS=${SAI_RUNTIME_REQUIRED_VARS:-OPENAI_API_KEY,SAI_SLACK_BOT_TOKEN,SAI_SLACK_APP_TOKEN}
export SAI_SLACK_SOCKET_HEARTBEAT_PATH="$HEARTBEAT_PATH"

exec "$SCRIPT_DIR/with_runtime_env.sh" \
  "$PYTHON_BIN" "$SCRIPT_DIR/run_slack_socket_mode.py"
