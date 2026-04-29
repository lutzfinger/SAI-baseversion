#!/bin/sh

# Run the structured local regression suite before major commits.
# This keeps the release gate visible and grouped by system area:
# - style/lint
# - type safety
# - core config/provider checks
# - safety/policy checks
# - connector and dataset checks
# - workflow/API/replay/live-runner checks

set -eu

PYTHON_BIN=${PYTHON_BIN:-.venv/bin/python}

printf '%s\n' \
  '[1/7] Generated tool overview check' \
  "      $PYTHON_BIN scripts/generate_tool_overview.py --check"
"$PYTHON_BIN" scripts/generate_tool_overview.py --check

printf '%s\n' \
  '[2/7] Ruff lint' \
  "      $PYTHON_BIN -m ruff check ."
"$PYTHON_BIN" -m ruff check .

printf '%s\n' \
  '[3/7] mypy type checks' \
  "      $PYTHON_BIN -m mypy app tests scripts"
"$PYTHON_BIN" -m mypy app tests scripts

printf '%s\n' \
  '[4/7] core config / prompt / provider tests' \
  "      $PYTHON_BIN -m pytest -n auto tests/test_tool_registry.py tests/test_loaders.py tests/test_prompt_hashes.py tests/test_llm_provider.py"
"$PYTHON_BIN" -m pytest -n auto \
  tests/test_tool_registry.py \
  tests/test_loaders.py \
  tests/test_prompt_hashes.py \
  tests/test_llm_provider.py

printf '%s\n' \
  '[5/7] safety / policy / approval tests' \
  "      $PYTHON_BIN -m pytest -n auto tests/test_safety_tools.py tests/test_security_protocols.py tests/test_approvals.py"
"$PYTHON_BIN" -m pytest -n auto \
  tests/test_safety_tools.py \
  tests/test_security_protocols.py \
  tests/test_approvals.py

printf '%s\n' \
  '[6/7] connector / dataset tests' \
  "      $PYTHON_BIN -m pytest -n auto tests/test_gmail_connector.py tests/test_gmail_fixture_builder.py tests/test_gmail_label_tool.py tests/test_email_classification_dataset.py tests/test_live_meeting_request_dataset.py tests/test_meeting_requests.py"
"$PYTHON_BIN" -m pytest -n auto \
  tests/test_gmail_connector.py \
  tests/test_gmail_fixture_builder.py \
  tests/test_gmail_label_tool.py \
  tests/test_email_classification_dataset.py \
  tests/test_live_meeting_request_dataset.py \
  tests/test_meeting_requests.py

printf '%s\n' \
  '[7/7] workflow / replay / UI / observability tests' \
  "      $PYTHON_BIN -m pytest -n auto tests/test_email_workflow.py tests/test_live_script_runners.py tests/test_meeting_followup_workflow.py tests/test_meeting_workflow.py tests/test_meeting_tools.py tests/test_task_assistant.py tests/test_api.py tests/test_replay.py tests/test_reflection.py tests/test_langsmith.py"
"$PYTHON_BIN" -m pytest -n auto \
  tests/test_email_workflow.py \
  tests/test_live_script_runners.py \
  tests/test_meeting_followup_workflow.py \
  tests/test_meeting_workflow.py \
  tests/test_meeting_tools.py \
  tests/test_task_assistant.py \
  tests/test_api.py \
  tests/test_replay.py \
  tests/test_reflection.py \
  tests/test_langsmith.py
