"""Regression runner for the sai-eval workflow itself.

Per principle #16d (every workflow gets the same shape) and the
operator's "EVAL FIRST" directive (PRINCIPLES.md §16a #1): the
sai-eval workflow needs its OWN regression set, separate from the
classifier canaries and the LLM edge cases.

What this catches:

  * Agent telling jokes instead of refusing (operator-reported regression)
  * Agent skipping `read_thread` and proposing against the latest reply
    instead of the first external sender
  * Agent proposing labels that don't exist in Gmail
  * Regex tier breaking on canonical patterns
  * System-prompt drift weakening hard refusals

Run modes:

  * **Default (offline, fast)** — uses a stub LLM that emits canned
    responses per case. Verifies tool wiring + outcome routing. Cheap,
    deterministic, runs in pytest. The cases tagged ``tier_under_test:
    rules`` skip the LLM entirely (regex tier).
  * **Live mode** (``SAI_REGRESSION_LIVE_LLM=1``) — calls the real
    Anthropic API + a stubbed Gmail. Costs ~$0.01 per case. Run on
    demand to catch real LLM-drift regressions.

The dataset itself lives at:
  - PUBLIC sample: ``app/agents/slack_eval_canaries.jsonl`` (placeholder
    data; ships with the framework as an example)
  - PRIVATE: ``eval/slack_eval_canaries.jsonl`` (operator's actual
    cases with their own contacts + labels — operator-curated)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

from pydantic import BaseModel, ConfigDict, Field

# Per principle #16a (revised 2026-05-03): the workflow regression
# dataset is now a generic WorkflowDataset[WorkflowCase]. SlackEvalCase
# is kept as an alias for back-compat; new workflows can use
# WorkflowCase directly or subclass it.
from app.eval.datasets import SlackEvalCase, WorkflowCase, WorkflowDataset

DEFAULT_PUBLIC_DATASET: Path = (
    Path(__file__).parent / "slack_eval_canaries.jsonl"
)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    actual_outcome: str
    tool_calls_made: list[str]
    operator_message: str
    fail_reason: Optional[str] = None


@dataclass
class RegressionReport:
    cases_total: int
    cases_passed: int
    cases_failed: int
    case_results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.cases_failed == 0


def load_cases(path: Path = DEFAULT_PUBLIC_DATASET) -> list[SlackEvalCase]:
    """Load JSONL test cases."""

    if not path.exists():
        return []
    out: list[SlackEvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(SlackEvalCase.model_validate_json(line))
    return out


# ─── stub Gmail ───────────────────────────────────────────────────────


def _build_stub_gmail_authenticator(
    *, gmail_labels: list[str], search_results: list[dict[str, Any]] | None = None,
) -> Any:
    """A Gmail authenticator that fakes labels.list + the connector
    paths used by our tools. Search/thread results are configurable
    per-case via the case fixture.
    """

    auth = MagicMock()
    service = MagicMock()
    auth.build_service.return_value = service
    service.users().labels().list().execute.return_value = {
        "labels": [{"id": f"id_{n}", "name": n} for n in gmail_labels],
    }
    auth._test_results = search_results or []
    auth._test_thread = []
    return auth


# ─── case runner ──────────────────────────────────────────────────────


def run_case(
    case: SlackEvalCase,
    *,
    gmail_labels: list[str],
    settings: Any,
    llm: Any | None = None,
    monkeypatch_target: Any | None = None,
) -> CaseResult:
    """Run one case. Used by both the offline and live regression paths."""

    if case.tier_under_test == "rules":
        return _run_regex_only(case)
    return _run_with_agent(
        case, gmail_labels=gmail_labels, settings=settings, llm=llm,
        monkeypatch_target=monkeypatch_target,
    )


def _run_regex_only(case: SlackEvalCase) -> CaseResult:
    """Tier-1-only path: just verify the regex parsers + outcome shape.
    No LLM, no Gmail, no agent. Cheap.
    """

    from app.eval.operator_patterns import (
        ParseError, parse_add_eval, parse_add_rule,
    )

    text = case.input_text

    rule = None
    rule_err = None
    try:
        rule = parse_add_rule(text, proposed_by="U_TEST")
    except ParseError as exc:
        rule_err = str(exc)

    eval_p = None
    try:
        eval_p = parse_add_eval(text, proposed_by="U_TEST")
    except ParseError as exc:
        rule_err = rule_err or str(exc)

    if rule is not None:
        actual = "proposed_classifier_rule"
    elif eval_p is not None:
        actual = "proposed_llm_example_or_clarify"
    else:
        actual = "no_match"

    return _judge(case, actual_outcome=actual, tool_calls_made=[],
                  operator_message="(regex tier — no agent message)")


def _run_with_agent(
    case: SlackEvalCase,
    *,
    gmail_labels: list[str],
    settings: Any,
    llm: Any | None = None,
    monkeypatch_target: Any | None = None,
) -> CaseResult:
    """Run the case through the agent. `llm=None` requires the live
    Anthropic API (and ANTHROPIC_API_KEY); pass a fake LLM for offline.
    """

    from app.agents.sai_eval_agent import run_agent

    auth = _build_stub_gmail_authenticator(gmail_labels=gmail_labels)
    proposed_dir = Path(getattr(settings, "tmpdir", "/tmp")) / "slack_eval_proposed"
    audit_path = proposed_dir / "audit.jsonl"

    result = run_agent(
        operator_user_id="U_TEST",
        source_text=case.input_text,
        proposed_dir=proposed_dir,
        gmail_authenticator=auth,
        llm=llm,
        audit_path=audit_path,
    )
    tool_calls = [
        tc.get("tool", "") for tc in (result.invocation.tool_calls if result.invocation else [])
    ]
    actual_outcome = _classify_actual_outcome(result)
    return _judge(
        case,
        actual_outcome=actual_outcome,
        tool_calls_made=tool_calls,
        operator_message=result.operator_message,
    )


def _classify_actual_outcome(agent_result: Any) -> str:
    if agent_result.staged_proposal_path:
        # Look at the staged proposal kind by reading the file.
        try:
            import yaml
            data = yaml.safe_load(Path(agent_result.staged_proposal_path).read_text())
            kind = (data or {}).get("kind", "")
            if kind == "rule_add":
                return "proposed_classifier_rule"
            if kind == "eval_add":
                return "proposed_llm_example"
        except Exception:
            pass
        return "proposed_unknown"
    msg_lower = (agent_result.operator_message or "").lower()
    if "label" in msg_lower and ("doesn't exist" in msg_lower or "not a gmail label" in msg_lower or "create it" in msg_lower):
        return "refused_label_missing"
    return "refused"


def _judge(
    case: SlackEvalCase, *,
    actual_outcome: str, tool_calls_made: list[str], operator_message: str,
) -> CaseResult:
    """Compare actual against expected; return a CaseResult."""

    fails: list[str] = []

    # Outcome check — accept "_or_*" as shorthand for "any of these".
    if not _outcome_matches(case.expected_outcome, actual_outcome):
        fails.append(
            f"outcome: expected={case.expected_outcome} got={actual_outcome}"
        )

    if case.expected_tool_calls:
        if tool_calls_made != case.expected_tool_calls:
            fails.append(
                f"tool_calls_exact: expected={case.expected_tool_calls} "
                f"got={tool_calls_made}"
            )

    if case.expected_tool_calls_subset:
        missing = [
            t for t in case.expected_tool_calls_subset
            if t not in tool_calls_made
        ]
        if missing:
            fails.append(f"tool_calls_subset_missing={missing}")

    for needle in case.expected_message_must_contain:
        if needle.lower() not in operator_message.lower():
            fails.append(f"message_must_contain={needle!r}")

    if case.expected_message_must_contain_any_of:
        any_match = any(
            needle.lower() in operator_message.lower()
            for needle in case.expected_message_must_contain_any_of
        )
        if not any_match:
            fails.append(
                f"message_must_contain_any_of={case.expected_message_must_contain_any_of}"
            )

    for needle in case.expected_message_must_not_contain:
        if needle.lower() in operator_message.lower():
            fails.append(f"message_must_not_contain={needle!r}")

    return CaseResult(
        case_id=case.case_id,
        passed=not fails,
        actual_outcome=actual_outcome,
        tool_calls_made=tool_calls_made,
        operator_message=operator_message[:300],
        fail_reason=("; ".join(fails) if fails else None),
    )


def _outcome_matches(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    # "_or_clarify" suffix means either outcome is OK.
    if "_or_" in expected:
        options = expected.split("_or_")
        # Reconstruct full outcome names from the split parts.
        # e.g. "proposed_llm_example_or_clarify" → ["proposed_llm_example", "clarify"]
        # First option is full; later options are bare suffixes.
        full_first = options[0]
        if actual == full_first:
            return True
        for alt in options[1:]:
            if actual == alt:
                return True
            # "refused" is the canonical "clarify" outcome
            if alt == "clarify" and actual == "refused":
                return True
    return False


def run_regression(
    *,
    dataset_path: Path = DEFAULT_PUBLIC_DATASET,
    gmail_labels: list[str] | None = None,
    llm: Any | None = None,
    settings: Any | None = None,
) -> RegressionReport:
    """Run the full regression. Returns a structured report."""

    cases = load_cases(dataset_path)
    if gmail_labels is None:
        gmail_labels = ["L1/Customers", "L1/Keynote", "L1/Partners"]
    if settings is None:
        from types import SimpleNamespace
        import tempfile
        settings = SimpleNamespace(tmpdir=tempfile.mkdtemp())

    results: list[CaseResult] = []
    for case in cases:
        try:
            r = run_case(
                case, gmail_labels=gmail_labels, settings=settings, llm=llm,
            )
        except Exception as exc:
            r = CaseResult(
                case_id=case.case_id,
                passed=False,
                actual_outcome="error",
                tool_calls_made=[],
                operator_message="",
                fail_reason=f"runner exception: {type(exc).__name__}: {exc}",
            )
        results.append(r)

    passed = sum(1 for r in results if r.passed)
    return RegressionReport(
        cases_total=len(results),
        cases_passed=passed,
        cases_failed=len(results) - passed,
        case_results=results,
    )


# ─── CLI entrypoint ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="slack_eval_regression",
        description="Run the slack-eval workflow regression set.",
    )
    p.add_argument("--dataset", default=str(DEFAULT_PUBLIC_DATASET),
                   help="Path to the JSONL test cases.")
    p.add_argument("--live", action="store_true",
                   help="Use real Anthropic API (cost ~$0.01/case).")
    args = p.parse_args(argv)

    llm = None
    if not args.live and os.environ.get("SAI_REGRESSION_LIVE_LLM") != "1":
        # Try to import a fake LLM. If unavailable, regex-only cases
        # still run; LLM cases come back with errors (clearly marked).
        llm = _build_default_offline_fake_llm()

    report = run_regression(dataset_path=Path(args.dataset), llm=llm)
    print(f"slack-eval regression: {report.cases_passed}/{report.cases_total} passed")
    for r in report.case_results:
        marker = "✓" if r.passed else "✗"
        print(f"  {marker} {r.case_id} → {r.actual_outcome}")
        if r.fail_reason:
            print(f"      FAIL: {r.fail_reason}")
    return 0 if report.passed else 1


def _build_default_offline_fake_llm() -> Any:
    """Stub LLM that returns a refusal with no tool calls.

    For LLM-tier cases this means the agent's output is "refused"
    — perfect for off-topic cases and useful as a baseline
    everywhere else (the test marks it as fail if a real propose
    was expected).
    """

    try:
        from langchain_core.language_models.fake_chat_models import (
            GenericFakeChatModel,
        )
        from langchain_core.messages import AIMessage
        return GenericFakeChatModel(messages=iter([
            AIMessage(content=(
                "That's outside what I do here — this channel is just "
                "for evaluation feedback. Try `add rule: ...` or "
                "`... should be ...`."
            )),
        ]))
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.exit(main())
