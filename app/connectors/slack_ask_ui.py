"""SlackAskUI — concrete AskPoster for the eval-channel Slack flow.

Implements the `AskPoster` Protocol from `app.runtime.ai_stack.tiers.human` and
posts a block-kit message to a single eval channel (default convention:
`#sai-eval`; the literal channel name lives in private overlay config and is
injected at construction time so the public starter ships only the protocol).

Flow:
  1. HumanTier (or AskOrchestrator) calls `post_ask(...)`.
  2. SlackAskUI builds a block-kit message with: task header, input summary,
     prior tier predictions, the question, and an instruction to reply in
     thread.
  3. Posts via WebClient.chat_postMessage; gets back a message `ts`.
  4. Persists an Ask record (status=OPEN) and returns its `ask_id`.
  5. Reconciliation (step 6) polls Slack threads for replies and updates
     the Ask + the linked EvalRecord(s).

The Slack token, channel name, and bot identity are constructor args. Tests
inject a mock `WebClient`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.eval.ask import Ask, AskKind, AskStatus, AskStore

if TYPE_CHECKING:
    from slack_sdk.web import WebClient

DEFAULT_REPLY_WINDOW = timedelta(days=3)


class SlackAskUI:
    """Posts asks to a single Slack channel, persists Ask records to disk."""

    def __init__(
        self,
        *,
        client: WebClient,
        channel: str,
        ask_store: AskStore,
        reply_window: timedelta = DEFAULT_REPLY_WINDOW,
        bot_user_id: str | None = None,
    ) -> None:
        self.client = client
        self.channel = channel
        self.ask_store = ask_store
        self.reply_window = reply_window
        self.bot_user_id = bot_user_id

    def post_ask(
        self,
        *,
        task_id: str,
        input_data: dict[str, Any],
        prior_predictions: dict[str, Any] | None = None,
        question_text: str | None = None,
        kind: AskKind = AskKind.CLASSIFICATION,
        record_ids: list[str] | None = None,
        options: list[str] | None = None,
    ) -> str:
        """Post an ask and return the persisted ask_id."""

        question = (
            question_text
            or "I'm unsure how to handle this. Could you decide and reply in thread?"
        )
        blocks = _build_blocks(
            task_id=task_id,
            input_data=input_data,
            prior_predictions=prior_predictions or {},
            question=question,
            options=options or [],
        )
        fallback_text = f"[{task_id}] {question}"

        response = self.client.chat_postMessage(
            channel=self.channel,
            blocks=blocks,
            text=fallback_text,
        )
        thread_ts = str(response.get("ts") or "")
        posted_at = datetime.now(UTC)

        ask = Ask(
            task_id=task_id,
            kind=kind,
            status=AskStatus.OPEN,
            record_ids=record_ids or [],
            question_text=question,
            options=options or [],
            free_form_allowed=True,
            posted_to_channel=self.channel,
            posted_to_thread_ts=thread_ts or None,
            posted_at=posted_at,
            expires_at=posted_at + self.reply_window,
            metadata={
                "input_summary": _summarize_input(input_data),
            },
        )
        self.ask_store.append(ask)
        return ask.ask_id


def _build_blocks(
    *,
    task_id: str,
    input_data: dict[str, Any],
    prior_predictions: dict[str, Any],
    question: str,
    options: list[str],
) -> list[dict[str, Any]]:
    """Build the block-kit payload for one ask.

    Layout:
      header              — "[task_id] needs input"
      section (markdown)  — input summary
      section (markdown)  — prior predictions (if any)
      section (markdown)  — the question + options + reply instruction
    """

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"[{task_id}] needs input",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Input:*\n```{_summarize_input(input_data)}```",
            },
        },
    ]
    if prior_predictions:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Prior tier predictions:*\n```"
                        + _summarize_predictions(prior_predictions)
                        + "```"
                    ),
                },
            }
        )
    body = f"*Question:* {question}"
    if options:
        body += "\n\n*Options:*\n" + "\n".join(f"• `{o}`" for o in options)
    body += "\n\n_Reply in this thread to answer._"
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        }
    )
    return blocks


def _summarize_input(input_data: dict[str, Any], *, max_chars: int = 800) -> str:
    """Short, deterministic JSON summary of an input dict for display."""

    serialized = json.dumps(input_data, indent=2, default=str, sort_keys=True)
    if len(serialized) > max_chars:
        return serialized[: max_chars - 4] + "\n…"
    return serialized


def _summarize_predictions(
    prior_predictions: dict[str, Any], *, max_chars: int = 600
) -> str:
    """Short, deterministic summary of tier predictions for display."""

    serialized = json.dumps(prior_predictions, indent=2, default=str, sort_keys=True)
    if len(serialized) > max_chars:
        return serialized[: max_chars - 4] + "\n…"
    return serialized
