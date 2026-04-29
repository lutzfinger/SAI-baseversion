#!/bin/sh

# Run a command with only local non-secret runtime env loaded.

set -eu

PLAIN_ENV_FILE=${PLAIN_ENV_FILE:-"$HOME/.config/sai/runtime.env"}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REQUIRED_RUNTIME_VARS=${SAI_RUNTIME_REQUIRED_VARS:-}

if [ "$#" -eq 0 ]; then
  echo "Usage: scripts/with_runtime_env.sh <command> [args...]" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$SCRIPT_DIR/runtime_secret_helpers.sh"
load_sai_runtime_env

if [ -n "$REQUIRED_RUNTIME_VARS" ]; then
  missing_vars=
  for var_name in $(printf '%s' "$REQUIRED_RUNTIME_VARS" | tr ',' ' '); do
    [ -n "$var_name" ] || continue
    eval "var_value=\${$var_name-}"
    if [ -z "$var_value" ]; then
      missing_vars="${missing_vars}${missing_vars:+, }$var_name"
    fi
  done
  if [ -n "$missing_vars" ]; then
    echo "Missing required runtime env vars: $missing_vars" >&2
    echo "Install or mirror the scheduler/runtime secrets into $PLAIN_ENV_FILE." >&2
    exit 1
  fi
fi

exec "$@"
