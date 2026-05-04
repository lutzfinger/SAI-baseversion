#!/bin/sh
# Install the Loop 1 pre-commit gate. Runs canary regression on every
# commit that touches rule or prompt files. Hard-fail blocks the commit.
#
# Per PRINCIPLES.md §16a Loop 1: every code edit affecting classification
# must pass canary regression before shipping.
#
# Install:
#   ./scripts/install_eval_hooks.sh                 # install in this repo
#   SAI_REPO=/path/to/private ./scripts/install_eval_hooks.sh

set -eu

REPO="${SAI_REPO:-$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)}"
HOOK_DIR="$REPO/.git/hooks"

if [ ! -d "$HOOK_DIR" ]; then
  echo "error: not a git repo: $REPO" >&2
  exit 2
fi

HOOK_PATH="$HOOK_DIR/pre-commit"

cat > "$HOOK_PATH" <<'HOOK'
#!/bin/sh
# Loop 1 pre-commit gate (PRINCIPLES.md §16a).
# Runs canary regression if any classification-affecting file is staged.

set -eu

# Files whose changes mean rules or LLM prompts may have shifted.
TRIGGER_PATTERNS='prompts/email/keyword-classify.md prompts/email/llm-classify\.md prompts/email/llm-classify-cloud.md prompts/email/llm-classify-local.md prompts/email/llm-classify-gptoss.md app/tools/keyword_classifier.py app/tasks/email_classification.py'

# Check if any staged file matches a trigger pattern.
TRIGGERED=0
for pattern in $TRIGGER_PATTERNS; do
  if git diff --cached --name-only | grep -q "$pattern"; then
    TRIGGERED=1
    echo "pre-commit: detected change to $pattern → running Loop 1 regression"
    break
  fi
done

if [ "$TRIGGERED" -eq 0 ]; then
  exit 0
fi

# Resolve venv python (private overlay venv preferred; fallback to system).
REPO_ROOT=$(git rev-parse --show-toplevel)
PY="${SAI_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "pre-commit: no venv python at $PY; skipping (set SAI_PYTHON to override)" >&2
  exit 0
fi

# Regenerate canaries from the (possibly edited) rules YAML, then run.
"$PY" -m scripts.sai_eval generate-canaries >/dev/null
if ! "$PY" -m scripts.sai_eval run --canaries-only; then
  echo
  echo "✗ canaries failed — commit blocked. Fix the rules tier first." >&2
  echo "  Run \`sai eval run --canaries-only\` to see details." >&2
  exit 1
fi

echo "✓ canaries pass — commit proceeding"
HOOK

chmod +x "$HOOK_PATH"
echo "installed pre-commit hook → $HOOK_PATH"
echo ""
echo "trigger patterns:"
echo "  prompts/email/keyword-classify.md"
echo "  prompts/email/llm-classify*.md"
echo "  app/tools/keyword_classifier.py"
echo "  app/tasks/email_classification.py"
echo ""
echo "Test:  touch prompts/email/keyword-classify.md && git add -u && git commit -m 'test'"
echo "Bypass: git commit --no-verify  (don't unless you know why)"
