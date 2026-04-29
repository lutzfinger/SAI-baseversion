#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON:-"$REPO_ROOT/.venv/bin/python"}
API_HOST=${SAI_API_HOST:-127.0.0.1}
API_PORT=${SAI_API_PORT:-8000}

export PYTHONUNBUFFERED=1

exec "$SCRIPT_DIR/with_runtime_env.sh" \
  "$PYTHON_BIN" -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT"
