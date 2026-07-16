#!/usr/bin/env bash
# principles_checkin.sh — the check-in layer SPINE (Plan F / N11 + Plan E / N10).
#
# ONE entrypoint that runs the EXISTING principle checks regardless of which repo
# hosts each, plus a few plan-time FLAG heuristics (E), and prints a single
# verdict. It composes existing primitives — it is NOT a new gate framework
# (DP1 reuse-not-rebuild; the exact thing the check-in layer exists to enforce):
#   - boundary_check.py           (base repo, #24 PII/secrets)
#   - check_hardcoded_models.py   (base repo, #24b)
#   - reuse_inventory_check.py    (plan-test-code skill, reuse-before-build)
#   - check_doc_freshness.py      (working repo, doc drift)
#   - E FLAG greps               (cascade run-all / keyword-routing / fail-closed)
#
# DP4 — it NEVER prints "aligned". Output is "N passed, M flags — mechanical
# only; adversarial/human review still required." Mechanical checks COMPLEMENT
# the judgment layer (frame review + human), they do not replace it. Novel /
# subtle violations are out of mechanical scope by construction.
#
# Usage: principles_checkin.sh [--repo <dir>] [--base <dir>] [--plan <file>]
# Env: SAI_BASEVERSION overrides --base. Exit 0 always (advisory aggregate);
#      individual blocking gates live in their own hooks/CI.
set -uo pipefail

# Operator paths come from env / are derived at runtime — never hardcoded here,
# so this script is boundary-clean and public-shippable (#17).
REPO="${SAI_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
BASE="${SAI_BASEVERSION:-$(dirname "$REPO")/SAI-baseversion}"
PLAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --plan) PLAN="$2"; shift 2 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
PYBIN="$BASE/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

PASS=0; FLAG=0; FAIL=0
row() { printf '%-5s %-18s %s\n' "$1" "$2" "$3"; case "$1" in PASS) PASS=$((PASS+1));; FLAG) FLAG=$((FLAG+1));; FAIL) FAIL=$((FAIL+1));; esac; }

# ── reused mechanisms ────────────────────────────────────────────────
bscript="$BASE/scripts/boundary_check.py"
if [ -f "$bscript" ]; then
  if (cd "$BASE" && "$PYBIN" "$bscript" >/dev/null 2>&1); then row PASS boundary "public repo PII-clean (#24)"; else row FAIL boundary "boundary_check FAILED — PII/secret in public repo"; fi
else row FLAG boundary "scanner not found at $bscript"; fi

mscript="$BASE/scripts/check_hardcoded_models.py"
if [ -f "$mscript" ]; then
  # NB: the linter writes violations to STDERR and signals via EXIT CODE
  # (0 clean, 1 violations) — rely on the exit code, not on grepping stdout.
  mout="$( (cd "$BASE" && "$PYBIN" "$mscript" 2>&1) )"; mrc=$?
  n="$(printf '%s\n' "$mout" | grep -oE 'Total violations: [0-9]+' | grep -oE '[0-9]+' | head -1)"
  if [ "$mrc" -eq 0 ]; then row PASS hardcoded-models "no literal model ids (#24b)"; else row FLAG hardcoded-models "${n:-some} literal model id(s) — advisory; see backlog loose-end"; fi
else row FLAG hardcoded-models "linter not found (#24b)"; fi

rcheck="$REPO/skills/plan-test-code/scripts/reuse_inventory_check.py"
if [ -n "$PLAN" ] && [ -f "$rcheck" ]; then
  if "$PYBIN" "$rcheck" "$PLAN" --repo "$REPO" >/dev/null 2>&1; then row PASS reuse-before-build "new primitives declared (or none)"; else row FAIL reuse-before-build "new primitive without a why-not-reuse decision"; fi
else row PASS reuse-before-build "n/a (no --plan given)"; fi

dcheck="$REPO/scripts/check_doc_freshness.py"
if [ -f "$dcheck" ]; then
  if "$PYBIN" "$dcheck" --repo "$REPO" >/dev/null 2>&1; then
    out="$("$PYBIN" "$dcheck" --repo "$REPO" 2>&1)"
    if printf '%s' "$out" | grep -q "possible STALE"; then row FLAG doc-freshness "owning doc(s) not updated for a changed area"; else row PASS doc-freshness "changed areas' docs updated"; fi
  else row PASS doc-freshness "ok"; fi
else row FLAG doc-freshness "checker not found"; fi

# ── E: plan-time FLAG heuristics over the working diff (advisory) ─────
changed="$(git -C "$REPO" diff --name-only HEAD~1 2>/dev/null; git -C "$REPO" diff --name-only 2>/dev/null; git -C "$REPO" diff --name-only --cached 2>/dev/null)"
pyfiles="$(printf '%s\n' "$changed" | sort -u | grep -E '\.py$' | grep -E '^app/' || true)"

# fail-closed heuristic: bare `except: pass` / silent swallow in changed app code
fc=0
for f in $pyfiles; do
  [ -f "$REPO/$f" ] && grep -nE 'except\s*:\s*$|except Exception:\s*$' "$REPO/$f" >/dev/null 2>&1 && fc=$((fc+1))
done
[ "$fc" -eq 0 ] && row PASS fail-closed "no bare-except in changed app code (#6)" || row FLAG fail-closed "$fc changed file(s) with a bare except — verify not silently swallowing"

# D1: new keyword/substring routing lists in changed classification code
kw=0
for f in $(printf '%s\n' "$pyfiles" | grep -iE 'classif|route|tag|triage' || true); do
  [ -f "$REPO/$f" ] && grep -nE 'in \[.*".*".*\]|startswith\(\(|any\(k in ' "$REPO/$f" >/dev/null 2>&1 && kw=$((kw+1))
done
[ "$kw" -eq 0 ] && row PASS keyword-routing "no new keyword-list routing (D1)" || row FLAG keyword-routing "$kw classification file(s) with literal keyword routing — should this be a rules-tier fact or the LLM? (D1)"

# cascade: run-all-to-compare (parallel tier invocation) in changed code
ca=0
for f in $pyfiles; do
  [ -f "$REPO/$f" ] && grep -nE 'for tier in .*tiers|run_all_tiers|gather\(.*tier' "$REPO/$f" >/dev/null 2>&1 && ca=$((ca+1))
done
[ "$ca" -eq 0 ] && row PASS cascade-earlystop "no run-all-to-compare pattern (#12)" || row FLAG cascade-earlystop "$ca file(s) may run all tiers — cascade must early-stop (#12)"

# least-privilege: needs a scope baseline to diff — not mechanized here
row FLAG least-privilege "not mechanized (needs granted-scope baseline) — human review (#19)"

# ── verdict (DP4: never 'aligned') ───────────────────────────────────
echo "----------------------------------------------------------------"
echo "principles check-in: ${PASS} passed, ${FLAG} flag(s), ${FAIL} fail(s)."
echo "MECHANICAL CHECKS ONLY — adversarial/human review is still required."
echo "(Novel or subtle principle violations are out of mechanical scope by"
echo " construction; a clean run is NOT a certificate of alignment.)"
[ "$FAIL" -gt 0 ] && echo ">> ${FAIL} hard FAIL(s) above must be resolved before shipping."
exit 0
