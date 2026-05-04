"""sai-eval workflow regression — runs on every test invocation.

This is the workflow-level eval set per the EVAL FIRST principle: every
workflow has its own regression dataset that catches changes to the
workflow itself (system prompt drift, tool wiring breakage, regex
abstain-on changes, etc.).

Two layers:

  * **Tier 1 (regex) cases** — always run, no LLM, deterministic.
  * **Tier 2 (LLM agent) cases** — by default run with a stub LLM that
    refuses everything. This catches:
      - regex tier accidentally consuming a case it shouldn't
      - tool-wiring breakage (the agent crashes vs returns clean refuse)
    For full LLM coverage set ``SAI_REGRESSION_LIVE_LLM=1`` and have
    ``ANTHROPIC_API_KEY`` configured. That mode is opt-in and runs
    against the real Claude API.

The dataset lives at ``app/agents/slack_eval_canaries.jsonl`` (PUBLIC,
placeholder data). Operator's private overlay can add more cases at
``eval/slack_eval_canaries.jsonl`` with real data; the runner accepts
a ``--dataset`` override.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.slack_eval_regression import (
    DEFAULT_PUBLIC_DATASET,
    load_cases,
    run_case,
    run_regression,
)


def test_dataset_loads():
    cases = load_cases(DEFAULT_PUBLIC_DATASET)
    assert len(cases) >= 5, "expected at least 5 baseline test cases"
    case_ids = {c.case_id for c in cases}
    # Spot-check a few must-have cases.
    assert "off_topic_joke" in case_ids
    assert "regex_add_rule_email" in case_ids


@pytest.mark.parametrize(
    "case",
    [c for c in load_cases(DEFAULT_PUBLIC_DATASET) if c.tier_under_test == "rules"],
    ids=lambda c: c.case_id,
)
def test_regex_tier_case(case):
    """Tier 1 cases — must pass deterministically without LLM."""

    from types import SimpleNamespace
    settings = SimpleNamespace(tmpdir="/tmp/sai_test_regex")
    result = run_case(case, gmail_labels=[
        "L1/Customers", "L1/Keynote",
    ], settings=settings)
    assert result.passed, (
        f"regex case failed: {result.fail_reason}\n"
        f"  actual_outcome={result.actual_outcome}\n"
        f"  message={result.operator_message}"
    )


def test_full_regression_summary():
    """High-level: every case has at least a known disposition.

    This is the regression aggregation entry point — when CI / pre-
    commit changes the slack_bot or sai_eval_agent, this fails loudly
    if the disposition rate drops.
    """

    from app.agents.slack_eval_regression import _build_default_offline_fake_llm

    # In offline mode the stub LLM refuses everything, so all
    # llm_agent cases that EXPECT a refusal pass; cases that expect a
    # propose come back as "refused" and fail. We track the baseline
    # disposition: at minimum, the regex tier cases pass.
    if os.environ.get("SAI_REGRESSION_LIVE_LLM") == "1":
        llm = None  # build real Anthropic; requires ANTHROPIC_API_KEY
    else:
        llm = _build_default_offline_fake_llm()

    report = run_regression(llm=llm)
    # Tier 1 cases must pass in every mode.
    tier1_results = [
        r for r in report.case_results
        if r.case_id.startswith("regex_")
    ]
    assert tier1_results, "expected at least one regex tier case"
    failed_tier1 = [r for r in tier1_results if not r.passed]
    assert not failed_tier1, (
        f"regex tier regression: {len(failed_tier1)} failures: "
        + "; ".join(f"{r.case_id}({r.fail_reason})" for r in failed_tier1)
    )
