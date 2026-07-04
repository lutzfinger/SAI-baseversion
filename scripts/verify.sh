#!/usr/bin/env bash
# scripts/verify.sh - local mirror of the CI shippability gate.
#
# Runs the same checks CI runs (.github/workflows/boundary.yml), so you can get
# CI's verdict before you push. Fail-fast and fail-closed: any stage that fails
# OR cannot run exits non-zero.
#
# COUPLING: this duplicates CI's check list. If boundary.yml changes, update this
# script. The proper DRY fix (CI calls this script) is a future increment and
# MUST preserve the branch-protection-required "boundary-check" status name.
#
# NOTE: the sample-skill stage runs `promote_skill --in-place`, which re-stamps
# the sample skill in the working tree (a no-op on a clean checkout).
set -uo pipefail

# Run from the repo root so the relative paths below resolve regardless of the
# caller's working directory. Fail closed if we are not inside the repo (an empty
# toplevel would make `cd ""` a silent no-op, so guard it explicitly).
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
    echo "verify: must be run inside the git repository." >&2
    exit 2
fi
cd "$ROOT" || { echo "verify: cannot enter repo root: $ROOT" >&2; exit 2; }

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY="$(command -v python3 || true)"; fi
fi
if [ -z "$PY" ] || ! "$PY" -c 'pass' >/dev/null 2>&1; then
    echo "verify: no working python interpreter - set PYTHON=... or run 'make install'." >&2
    exit 2
fi

fail() {
    echo "verify: FAILED at stage: $1" >&2
    [ -n "${2:-}" ] && echo "verify: hint: $2" >&2
    exit 1
}

echo "verify: [1/3] boundary linter"
"$PY" scripts/boundary_check.py \
    || fail "boundary linter" "fix the flagged term, or add the path to boundary_check_allowlist.txt"

echo "verify: [2/3] framework regression"
"$PY" -m pytest -n auto \
    tests/test_cascade_runner.py \
    tests/test_cascade_framework_e2e.py \
    tests/test_proposal_intake.py \
    tests/test_proposal_apply_eval_add.py \
    tests/test_skill_manifest.py \
    tests/test_skill_integrity.py \
    tests/test_sai_eval_tools.py \
    tests/test_slack_response_models.py \
    tests/test_boundary_guard_blocks.py \
    tests/test_danger_guard_blocks.py \
    tests/test_cross_review.py \
    tests/canonical/ \
    || fail "framework regression" "run 'make install' if this is an import or dependency error"

echo "verify: [3/3] sample-skill integrity + cascade e2e"
"$PY" -m scripts.promote_skill --in-place \
    --incoming-dir app/skills/sample_echo_skill \
    --target-dir   app/skills/sample_echo_skill \
    || fail "sample-skill promote/validate"
git diff --exit-code app/skills/sample_echo_skill/.skill-content-sha256 \
    || fail "sample-skill integrity hash drifted" "re-run promote_skill --in-place and commit the re-stamp, or revert the edit"
"$PY" -m pytest -n auto tests/test_cascade_framework_e2e.py \
    || fail "sample-skill cascade e2e"

echo "verify: PASS - all stages green (local mirror of CI)."
exit 0
