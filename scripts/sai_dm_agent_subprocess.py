#!/usr/bin/env python3
"""Subprocess wrapper for `run_dm_agent`.

Why: the LangChain `create_agent` + LangGraph + asyncio stack does not
mix cleanly with slack_bolt's worker-thread pool. Even when we spawn a
daemon thread for `agent.invoke()`, LangChain's internal async runner +
the cascade's import-time machinery deadlock against the bolt event
loop's locks. The CLI works because it's a single-threaded blocking
process. So: do the agent work in a *separate process*. True
isolation, no thread state inherited from bolt.

Protocol:
  - stdin (JSON): {"operator_user_id": "...", "source_text": "..."}
  - stdout (JSON): the DmAgentResult dict (operator_message,
                   staged_proposal_path, invocation summary)
  - non-zero exit if the agent itself raised before returning a result

Slack handler:
  1. :eyes: reaction (in-thread, before launch)
  2. subprocess.Popen of this script
  3. wait in a daemon thread; parse stdout; post reply + reactions
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Resolve env so SAI_GRANOLA_API_KEY / ANTHROPIC_API_KEY are populated
# (when this subprocess is spawned without inheriting the bolt env, or
# when called from the CLI for tests).
SAI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAI_ROOT))
from app.shared.runtime_env import load_runtime_env_best_effort  # noqa: E402
load_runtime_env_best_effort()


# ── Secret pre-flight (PRINCIPLES.md #6 fail closed + #7a 1P service-account)
#
# The bot's parent inherits ANTHROPIC_API_KEY via with_1password.sh. Two
# ways the subprocess can land here with the key missing-or-empty:
#   (a) `op inject` returned an empty value during a network blip at bot
#       startup and SAI_OP_REQUIRE_NONEMPTY wasn't set on the wrapper;
#   (b) someone invoked this subprocess directly without sourcing runtime.env.
# Both surface as the Anthropic SDK's generic "Could not resolve
# authentication method" — opaque to the operator (we hit this exact bug
# 2026-05-20 night; see ~/Lutz_Dev/SAI/docs/dm-agent-fix-2026-05-21/).
#
# We pre-check here and emit a recovery-oriented stdout JSON instead.
# The slack handler posts the friendly message to the DM thread, so the
# operator sees an actionable hint, not a stack trace.
REQUIRED_SECRETS: tuple[str, ...] = ("ANTHROPIC_API_KEY",)


def _missing_secrets() -> list[str]:
    return [name for name in REQUIRED_SECRETS if not (os.environ.get(name) or "").strip()]


def _emit_missing_secrets_payload(missing: list[str]) -> None:
    listing = ", ".join(missing)
    print(json.dumps({
        "operator_message": (
            f":x: My API credentials didn't load (missing/empty: {listing}).\n\n"
            f"This usually means `op inject` returned empty during a network "
            f"blip when the bot started. To fix:\n"
            f"```\n"
            f"launchctl bootout gui/$(id -u)/com.sai.slack-bot && \\\n"
            f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sai.slack-bot.plist\n"
            f"```\n"
            f"The plist now exports `SAI_OP_REQUIRE_NONEMPTY=1` so the bot "
            f"refuses to start with empty secrets — launchd retries every 30s "
            f"until network + 1Password resolve cleanly."
        ),
        "staged_proposal_path": None,
        "invocation": {
            "invocation_id": None,
            "iterations": 0,
            "terminated_reason": "missing_secrets",
            "error": f"missing or empty: {listing}",
        },
        "subprocess_error": True,
    }))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        print(json.dumps({
            "operator_message": f":x: subprocess: bad stdin: {exc}",
            "staged_proposal_path": None,
            "invocation": None,
            "subprocess_error": True,
        }))
        return 2

    operator_user_id = str(payload.get("operator_user_id", ""))
    source_text = str(payload.get("source_text", ""))
    if not operator_user_id or not source_text:
        print(json.dumps({
            "operator_message": ":x: subprocess: missing operator_user_id or source_text",
            "staged_proposal_path": None,
            "invocation": None,
            "subprocess_error": True,
        }))
        return 2

    # ── pre-flight: required secrets present? ───────────────────────
    missing = _missing_secrets()
    if missing:
        _emit_missing_secrets_payload(missing)
        return 3   # distinct exit code so slack handler can log specifically

    # ── selftest mode: handshake without running the LLM ────────────
    # The bot uses this on startup (Fix 4) to confirm the subprocess path
    # is reachable + secrets resolved BEFORE the operator sends the first
    # DM. Returns a benign JSON immediately without importing the agent.
    if source_text == "__selftest__":
        print(json.dumps({
            "operator_message": ":white_check_mark: selftest ok",
            "staged_proposal_path": None,
            "invocation": {
                "invocation_id": "selftest",
                "iterations": 0,
                "terminated_reason": "selftest_ok",
                "error": None,
            },
            "subprocess_error": False,
        }))
        return 0

    # Defer the agent import so the secret check above fires FIRST. langchain
    # reads ANTHROPIC_API_KEY at model instantiation; if empty, importing the
    # agent module would crash before we could emit a friendly payload.
    try:
        from app.agents.sai_operator_dm_agent import run_dm_agent  # noqa: E402
        result = run_dm_agent(
            operator_user_id=operator_user_id,
            source_text=source_text,
        )
    except Exception as exc:
        print(json.dumps({
            "operator_message": f":x: subprocess: run_dm_agent raised {type(exc).__name__}: {exc}",
            "staged_proposal_path": None,
            "invocation": None,
            "subprocess_error": True,
        }))
        return 1

    out = {
        "operator_message": result.operator_message,
        "staged_proposal_path": result.staged_proposal_path,
        "invocation": {
            "invocation_id": result.invocation.invocation_id if result.invocation else None,
            "iterations": result.invocation.iterations if result.invocation else 0,
            "terminated_reason": result.invocation.terminated_reason if result.invocation else "?",
            "error": result.invocation.error if result.invocation else None,
        } if result.invocation else None,
        "subprocess_error": False,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
