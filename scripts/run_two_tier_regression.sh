#!/bin/sh
# Two-tier regression test (PRINCIPLES.md, 2026-05-01).
#
# Runs in cascade order:
#   1. Classifier canaries — one synthetic test per rule. Hard fail
#      on any miss. Catches accidental rule deletions, threshold
#      drift, mechanism regressions.
#   2. LLM edge-case regression — replay every operator-curated edge
#      case through the cascade; report P/R/F1.
#
# If canaries fail, LLM eval is skipped — fix the rules tier first.
#
# Usage:
#   ./scripts/run_two_tier_regression.sh
#   ./scripts/run_two_tier_regression.sh --report-dir /tmp/reg-2026-05-01

set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

REPORT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --report-dir) REPORT_DIR="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [ -n "$REPORT_DIR" ]; then
  mkdir -p "$REPORT_DIR"
  CANARY_REPORT="--report-out $REPORT_DIR/canaries.json"
  LLM_REPORT="--report-out $REPORT_DIR/llm_regression.json"
else
  CANARY_REPORT=""
  LLM_REPORT=""
fi

PY="${SAI_PYTHON:-$REPO_ROOT/.venv/bin/python}"

echo "═══════════════════════════════════════════════════════════════"
echo "TIER 1: classifier canaries"
echo "═══════════════════════════════════════════════════════════════"
# shellcheck disable=SC2086
if ! "$PY" -m scripts.regression_test_canaries $CANARY_REPORT; then
  echo
  echo "✗ canaries failed — stopping. Fix the rules tier first."
  echo "  (LLM regression skipped to avoid masking the rule break.)"
  exit 1
fi

echo
echo "═══════════════════════════════════════════════════════════════"
echo "TIER 2: LLM edge-case regression"
echo "═══════════════════════════════════════════════════════════════"
# shellcheck disable=SC2086
"$PY" -m scripts.regression_test_email_classifier $LLM_REPORT

echo
echo "✓ two-tier regression complete"
