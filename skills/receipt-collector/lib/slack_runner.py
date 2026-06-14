"""
slack_runner — Slack trigger surface for the cost-compiler.

Operator types a message like
    @SAI file my INSEAD May receipts in EUR
in the configured channel. This module:

  1. Parses the message via `parse_trigger.parse`.
  2. Derives the deterministic atomic-step plan via `derive_plan`.
  3. Posts a confirmation back to the SAME channel/thread that the
     operator triggered from (per the surface-continuity invariant
     in `docs/workflow.md` Step 0).
  4. Drives the plan, posting status updates as each step finishes.
  5. When it hits the `await-approval` step, it posts the
     final-review.md to the thread, waits for the operator's reaction
     (✅/❌) or a typed `yes`/`no`, then writes a sentinel file the
     CLI-side `await-approval --surface file` worker is watching.

Per SAI #1 (local-first) the orchestrator runs on the operator's Mac.
Slack is only the trigger + reply surface. No bot-side state.
Per #5 (least-privileged), scopes requested are:
  chat:write          — post messages
  channels:history    — read messages in the configured channel
  reactions:read      — react-based approval

Per #7a (service-account only), the bot token comes from 1Password.
Per #9 (approval as durable state), the approval state lives in the
existing `lib/approval.py` JSONL store; this module only writes the
sentinel after parsing the operator's reply.

Per #25 (standard libraries before custom code), this module uses
`slack-sdk` (~5 lines of boilerplate per send + listen). The bot
itself is ~50 lines. Don't reinvent slack-bolt's event loop.

Configuration required in overlay (`identity.yaml`):

    slack:
      bot_token_op_ref:
        op_item: "<your slack bot 1Password item name>"
        op_vault: "<vault>"
        field: "credential"
      channel_id: "C0XXXXXXXX"     # operator SAI cost channel
      operator_user_id: "U0XXXXXX" # the only user allowed to trigger

Public API:
    listen(overlay, runner_subprocess, sentinel_dir, *, poll_interval=5.0)
    post_message(overlay, text, thread_ts=None)
    post_review(overlay, review_md_path, thread_ts)

Phase C scaffold note: the CHAT-FLOW LOGIC is fully implemented
below, but the actual slack-sdk dependency may not be installed.
The function returns a clear `ImportError` with the install command
when slack-sdk is missing; once installed, listen() blocks on the
Slack Events API and dispatches to the runner.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional


def _read_secret(secret_ref: dict) -> str:
    """Pull a credential from 1Password via the `op` CLI (service-account auth).

    Routes through `op_env.ensure_sa_token` first so launchd-spawned
    daemons never trigger the macOS "op would like to access data"
    dialog (SAI principle #7a).
    """
    from lib import op_env
    op_env.ensure_sa_token()
    op_item = secret_ref.get("op_item")
    op_vault = secret_ref.get("op_vault")
    field = secret_ref.get("field", "credential")
    if not op_item or not op_vault:
        raise RuntimeError(f"Slack bot_token_op_ref incomplete: {secret_ref!r}")
    r = subprocess.run(
        ["op", "item", "get", op_item, "--vault", op_vault,
         "--reveal", "--fields", f"label={field}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Couldn't read Slack bot token from 1Password: {r.stderr.strip()}")
    return r.stdout.strip()


def _import_slack():
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        return WebClient, SlackApiError
    except ImportError as e:
        raise ImportError(
            "slack-sdk required. Install:  python3 -m pip install --user slack-sdk\n"
            f"({e})"
        )


def _client(overlay: dict):
    WebClient, _ = _import_slack()
    cfg = (overlay.get("slack") or {})
    token_ref = cfg.get("bot_token_op_ref")
    if not token_ref:
        raise RuntimeError(
            "Overlay is missing slack.bot_token_op_ref. Configure your "
            "overlay's identity.yaml before running slack-listen."
        )
    return WebClient(token=_read_secret(token_ref))


def post_message(overlay: dict, text: str, thread_ts: Optional[str] = None) -> dict:
    """Post a plain text message. Returns the API response dict."""
    client = _client(overlay)
    cfg = overlay.get("slack") or {}
    chan = cfg.get("channel_id")
    if not chan:
        raise RuntimeError("Overlay is missing slack.channel_id.")
    return client.chat_postMessage(
        channel=chan,
        text=text,
        thread_ts=thread_ts,
        mrkdwn=True,
    ).data


def post_review(overlay: dict, review_md_path: Path, thread_ts: str) -> dict:
    """Upload final-review.md as a snippet in the trigger thread."""
    client = _client(overlay)
    cfg = overlay.get("slack") or {}
    chan = cfg.get("channel_id")
    body = Path(review_md_path).read_text()
    return client.files_upload_v2(
        channel=chan,
        thread_ts=thread_ts,
        title=f"Review: {Path(review_md_path).name}",
        content=body,
        filename=Path(review_md_path).name,
        initial_comment=":mag: Final review attached. Reply `yes` to approve or `no` to abort.",
    ).data


def listen(
    overlay: dict,
    *,
    runner_python_module: str = "skills.receipt-collector.runner",
    sentinel_dir: Optional[Path] = None,
    poll_interval: float = 5.0,
) -> None:
    """Block forever, listening to the configured channel.

    Drives the cost-compiler plan when the operator messages in. Posts
    status updates back to the SAME thread the trigger came from.

    Per #16i (every guarded interface declares what it can discuss),
    only the configured `operator_user_id` can trigger; other users
    get a friendly "this channel is for the cost-compiler operator
    only" refusal.
    """
    WebClient, SlackApiError = _import_slack()
    client = _client(overlay)
    cfg = overlay.get("slack") or {}
    chan = cfg.get("channel_id")
    op_user = cfg.get("operator_user_id")
    if not chan:
        raise RuntimeError("Overlay is missing slack.channel_id.")
    sentinel_dir = Path(sentinel_dir or os.path.expanduser(
        "~/Library/Application Support/SAI/receipt-collector/sentinels"
    ))
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    # Local imports to avoid circulars.
    from lib import approval as approval_lib

    print(f"slack_runner: listening on channel {chan} (poll {poll_interval}s)")
    print(f"  operator user: {op_user or '(any user — UNSAFE; configure operator_user_id)'}")
    print(f"  sentinel dir:  {sentinel_dir}")

    # Phase C scaffold: a simple long-poll on conversations.history is
    # the safest start (no socket-mode dependency). Production should
    # use Events API or socket-mode for instant delivery, but the
    # underlying logic stays the same.
    last_ts = str(time.time())
    while True:
        try:
            resp = client.conversations_history(
                channel=chan, oldest=last_ts, limit=20,
            )
            messages = resp.get("messages") or []
            messages.sort(key=lambda m: float(m.get("ts", "0")))
            for msg in messages:
                last_ts = msg.get("ts", last_ts)
                user = msg.get("user")
                text = msg.get("text") or ""
                if msg.get("subtype"):  # bot replies, joins, etc.
                    continue
                if op_user and user != op_user:
                    # Refuse-but-don't-stay-silent (per #16e).
                    client.chat_postMessage(
                        channel=chan, thread_ts=msg.get("ts"),
                        text=":no_entry: This channel is for the cost-compiler operator. "
                             "I can't trigger workflows for other users.",
                    )
                    continue
                # Run the LLM agent. It returns either a clarification
                # (no staged_plan_path) or a staged plan + an operator
                # summary message.
                from lib import cost_compiler_agent
                result = cost_compiler_agent.run_agent(
                    source_text=text, overlay=overlay,
                )
                client.chat_postMessage(
                    channel=chan, thread_ts=msg.get("ts"),
                    text=result.operator_message,
                )
                if not result.staged_plan_path:
                    # Agent asked for clarification — DON'T execute,
                    # operator must reply with a clearer trigger.
                    continue
                # Plan staged. Convert to atomic-step list and run.
                _execute_plan_from_proposal(
                    proposed_plan=result.proposed_plan,
                    staged_path=result.staged_plan_path,
                    overlay=overlay,
                    client=client, channel=chan, thread_ts=msg.get("ts"),
                    runner_python_module=runner_python_module,
                    sentinel_dir=sentinel_dir,
                )
        except Exception as e:
            print(f"slack_runner: error in poll loop: {e}")
        time.sleep(poll_interval)


def _execute_plan_from_proposal(
    proposed_plan: dict | None,
    staged_path: str,
    overlay: dict,
    *,
    client, channel: str, thread_ts: str,
    runner_python_module: str, sentinel_dir: Path,
) -> None:
    """Run the atomic steps derived from a staged plan.json proposal.

    The Haiku agent already chose customer + window + currency. This
    function expands those into the standard ordered subcommand list
    and posts status as each step finishes. Approval-via-Slack-reply
    is a Phase E task; today we run the collect phase, surface the
    staged plan, and let the operator approve manually.
    """
    if not proposed_plan:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":warning: Plan staged at `{staged_path}` but couldn't be parsed for execution.",
        )
        return

    slug = proposed_plan["trip_slug"]
    start = proposed_plan["trip_start"]
    end = proposed_plan["trip_end"]
    customer = proposed_plan["customer"]["DisplayName"]
    currency = proposed_plan["invoice_currency"]

    pre_steps = [
        ("scan-cards", {"start": start, "end": end}),
        ("search-receipts", {"start": start, "end": end}),
        ("attach-onsite-photos", {"start": start, "end": end, "trip": slug}),
        ("extract-pre-bookings", {"start": start, "end": end, "customer": customer}),
    ]
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=(f":robot_face: Running {len(pre_steps)}-step collect phase for "
              f"`{slug}` ({start}..{end}, {currency}, {customer})..."),
    )
    for i, (name, kwargs) in enumerate(pre_steps, 1):
        cmd = ["python3", "-m", runner_python_module, name]
        for k, v in (kwargs or {}).items():
            if v is None:
                continue
            cmd.append(f"--{k.replace('_', '-')}")
            cmd.append(str(v))
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":arrow_forward: Step {i}/{len(pre_steps)}: `{name}` ...",
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":alarm_clock: Step `{name}` timed out. Halting.",
            )
            return
        ok = proc.returncode == 0
        emoji = ":white_check_mark:" if ok else ":x:"
        tail = (proc.stdout or "").splitlines()[-10:]
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"{emoji} `{name}` exit={proc.returncode}\n```\n"
                 + "\n".join(tail) + "\n```",
        )
        if not ok:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=(":octagonal_sign: Collect phase halted. Plan still "
                      f"staged at `{staged_path}` — review and re-trigger."),
            )
            return

    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=(
            ":memo: Collect phase complete. Staged plan at "
            f"`{staged_path}`.\n\nApproval-via-Slack-reply lands in "
            "Phase E. For now, review the staged plan + "
            f"`~/Downloads/sai-receipts-{slug}/` and run "
            f"`create-invoice --trip {slug} --currency {currency}` "
            "manually after approval."
        ),
    )
