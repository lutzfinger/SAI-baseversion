#!/usr/bin/env bash
# escape_with_review.sh - a bounded-loop escape check that gates on BOTH the eval
# AND a different-vendor review. Use it as bounded_loop.py --escape-cmd for a
# review-gated loop:
#
#   FILE=<changed file> EVAL_CMD='.venv/bin/python -m pytest -q' \
#     scripts/escape_with_review.sh
#
# Exit 0 ONLY if the eval passes AND cross_review --fail-on high finds no HIGH.
# Otherwise exit 1 (the loop keeps iterating, bounded by its caps) with a logged
# reason: eval failed / review flagged HIGH / review tool error (fail-closed - it
# never claims reviewed-ok if the reviewer did not run).
#
# Provide OPENAI_API_KEY via your 1Password wrapper. Review-gated loops need a
# higher iteration budget, and gate at high so a nitpick MEDIUM does not stall them.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
EVAL_CMD="${EVAL_CMD:?set EVAL_CMD to your eval command}"
FILE="${FILE:?set FILE to the changed file to review}"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY=python3; fi
fi

if ! eval "$EVAL_CMD"; then
    echo "escape blocked: eval failed" >&2
    exit 1
fi

"$PY" "$ROOT/scripts/cross_review.py" \
    --file "$FILE" \
    --context "a change produced by an autonomous loop iteration" \
    --focus "correctness and safety regressions introduced by this change" \
    --fail-on high
code=$?
case "$code" in
    0) echo "escape ok: eval green and review clean" >&2; exit 0 ;;
    1) echo "escape blocked: review flagged HIGH (keep iterating)" >&2; exit 1 ;;
    *) echo "escape blocked: review tool error (exit $code) - fail-closed" >&2; exit 1 ;;
esac
