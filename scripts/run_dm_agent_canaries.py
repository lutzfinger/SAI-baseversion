#!/usr/bin/env python3
"""Canary runner for the SAI operator DM agent.

Per PRINCIPLES.md #16d (every workflow gets the same shape — no
exceptions) and #33 (skill plug-in protocol hard contract): the DM
agent workflow MUST own canaries that exercise its operator-visible
surface. Without them, regressions like the 2026-05-20 night failure —
``op inject`` returning empty during a network blip, causing the
subprocess to error with the opaque "Could not resolve authentication
method" instead of a recovery hint — would land undetected.

Two case modes:

  * ``mode: deterministic`` — no LLM call. Runs every time. These are
    the canaries that protect the framework-universal guardrails
    (pre-flight secret check, selftest handshake). Used by Loop 1
    (pre-ship regression) and by the bot's own startup self-test.
  * ``mode: live`` — calls the real Anthropic API via the subprocess
    wrapper. Costs ~$0.005 per case. Opt-in via
    ``SAI_DM_AGENT_LIVE=1``. Used when reviewing prompt edits or
    agent behaviour drift.

Exit codes:
  * 0 = all cases passed (or live skipped per env)
  * 1 = at least one case failed
  * 2 = configuration error (missing dataset, malformed cases)

Per PRINCIPLES.md #33b — this is an execution-layer regression runner,
NOT a redesign of the DM agent. The agent itself is unchanged; we just
exercise its observable behaviour.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SAI_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = SAI_ROOT / "eval" / "sai_operator_dm_agent_canaries.jsonl"
SUBPROCESS_SCRIPT = SAI_ROOT / "scripts" / "sai_dm_agent_subprocess.py"
VENV_PYTHON = SAI_ROOT / ".venv" / "bin" / "python3.12"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    actual_exit_code: int
    actual_terminated_reason: Optional[str]
    actual_message: str
    actual_staged_proposal_path: Optional[str]
    fail_reasons: list[str] = field(default_factory=list)


def _load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for n, raw in enumerate(path.read_text().splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            cases.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"line {n}: bad JSON: {exc}")
    return cases


def _invoke_subprocess(*, input_text: str, scrub_env: list[str] | None = None,
                      operator_user_id: str = "U_canary",
                      timeout: int = 60) -> dict:
    """Spawn the subprocess wrapper exactly as the slack bot would.

    Optionally scrubs specific env vars to simulate failure modes
    (e.g. missing ANTHROPIC_API_KEY).
    """
    env = {**os.environ}
    # Subprocess wrapper calls load_runtime_env_best_effort(); to
    # simulate a TRULY missing secret we must prevent the loader from
    # re-resolving via op CLI. Pointing PLAIN_ENV_FILE at a path that
    # doesn't exist disables the runtime.env fallback. Removing the
    # service-account token additionally prevents `op` from being able
    # to resolve `op://` refs even if the loader tried.
    if scrub_env:
        for key in scrub_env:
            env.pop(key, None)
        env["PLAIN_ENV_FILE"] = "/tmp/sai-canary-nonexistent.env"
        env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)

    venv = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    payload = json.dumps({
        "operator_user_id": operator_user_id,
        "source_text": input_text,
    })
    proc = subprocess.run(
        [str(venv), str(SUBPROCESS_SCRIPT)],
        input=payload, capture_output=True, text=True,
        timeout=timeout, cwd=str(SAI_ROOT), env=env,
    )
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {"_unparseable_stdout": proc.stdout}
    return {
        "exit_code": proc.returncode,
        "stdout_parsed": out,
        "stderr_tail": proc.stderr[-400:] if proc.stderr else "",
    }


def _check_case(case: dict, result: dict) -> CaseResult:
    case_id = case["case_id"]
    out = result["stdout_parsed"] or {}
    inv = (out.get("invocation") or {})
    actual = CaseResult(
        case_id=case_id,
        passed=True,
        actual_exit_code=result["exit_code"],
        actual_terminated_reason=inv.get("terminated_reason"),
        actual_message=out.get("operator_message", ""),
        actual_staged_proposal_path=out.get("staged_proposal_path"),
    )

    if "expected_exit_code" in case:
        if actual.actual_exit_code != case["expected_exit_code"]:
            actual.passed = False
            actual.fail_reasons.append(
                f"exit_code: expected {case['expected_exit_code']}, "
                f"got {actual.actual_exit_code}"
            )

    if "expected_terminated_reason" in case:
        if actual.actual_terminated_reason != case["expected_terminated_reason"]:
            actual.passed = False
            actual.fail_reasons.append(
                f"terminated_reason: expected "
                f"{case['expected_terminated_reason']!r}, got "
                f"{actual.actual_terminated_reason!r}"
            )

    if "expected_staged_proposal_path_present" in case:
        wanted = case["expected_staged_proposal_path_present"]
        got = bool(actual.actual_staged_proposal_path)
        if wanted != got:
            actual.passed = False
            actual.fail_reasons.append(
                f"staged_proposal_path: expected present={wanted}, got "
                f"present={got} (path={actual.actual_staged_proposal_path!r})"
            )

    if "expected_message_must_contain_any_of" in case:
        needles = case["expected_message_must_contain_any_of"]
        msg = (actual.actual_message or "").lower()
        if not any(n.lower() in msg for n in needles):
            actual.passed = False
            actual.fail_reasons.append(
                f"operator_message missing any of {needles!r}: "
                f"{(actual.actual_message or '')[:200]!r}"
            )

    if "expected_message_must_not_contain" in case:
        forbidden = case["expected_message_must_not_contain"]
        msg = (actual.actual_message or "").lower()
        hits = [f for f in forbidden if f.lower() in msg]
        if hits:
            actual.passed = False
            actual.fail_reasons.append(
                f"operator_message contained forbidden tokens: {hits!r}"
            )

    return actual


def _print_summary(results: list[CaseResult]) -> int:
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print()
    print(f"==== DM-agent canary summary: {passed}/{len(results)} passed ====")
    for r in results:
        mark = "✅" if r.passed else "❌"
        print(f"  {mark} {r.case_id}")
        if not r.passed:
            for reason in r.fail_reasons:
                print(f"      - {reason}")
            print(f"      actual_message: {(r.actual_message or '')[:200]!r}")
    print()
    return 0 if failed == 0 else 1


def main() -> int:
    if not DEFAULT_DATASET.exists():
        print(f"dataset not found: {DEFAULT_DATASET}", file=sys.stderr)
        return 2
    if not SUBPROCESS_SCRIPT.exists():
        print(f"subprocess wrapper missing: {SUBPROCESS_SCRIPT}", file=sys.stderr)
        return 2

    cases = _load_cases(DEFAULT_DATASET)
    if not cases:
        print(f"no cases in {DEFAULT_DATASET}", file=sys.stderr)
        return 2

    run_live = os.environ.get("SAI_DM_AGENT_LIVE", "").strip() in ("1", "true", "yes")
    results: list[CaseResult] = []

    for case in cases:
        mode = case.get("mode", "deterministic")
        if mode == "live" and not run_live:
            print(f"⏭  skipping live case {case['case_id']} "
                  f"(SAI_DM_AGENT_LIVE not set)")
            continue
        print(f"→ {case['case_id']}  ({mode})")
        try:
            result = _invoke_subprocess(
                input_text=case["input_text"],
                scrub_env=case.get("scrub_env"),
                timeout=case.get("timeout_seconds", 60),
            )
        except subprocess.TimeoutExpired:
            results.append(CaseResult(
                case_id=case["case_id"],
                passed=False,
                actual_exit_code=-1,
                actual_terminated_reason=None,
                actual_message="<subprocess timeout>",
                actual_staged_proposal_path=None,
                fail_reasons=["subprocess timed out"],
            ))
            continue
        results.append(_check_case(case, result))

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
