"""SlackAskUI — concrete AskPoster for the eval-channel Slack flow.

Implements the `AskPoster` Protocol from `app.runtime.ai_stack.tiers.human` and
posts a block-kit message to a single eval channel. The channel name lives in
private overlay config and is injected at construction time, so the public
starter ships only the protocol and the convention.

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
      section (markdown)  — pretty-formatted input summary
                             (email-aware when input has from_email/subject)
      section (markdown)  — top tier prediction (one-liner) if any
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
                "text": _format_input_section(input_data),
            },
        },
    ]
    top_prediction = _format_top_prediction(prior_predictions)
    if top_prediction:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": top_prediction},
            }
        )
    body = f"*Question:* {question}"
    if options:
        body += "\n\n*Options:* " + " ".join(f"`{o}`" for o in options)
    body += "\n\n_Reply in this thread to answer._"
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        }
    )
    return blocks


def _format_input_section(input_data: dict[str, Any]) -> str:
    """Pretty-print an input. Detects email shape and renders human-readable.

    Email shape: input_data has any of from_email / subject / snippet.
    Renders as labeled fields (From, To, Subject, Summary). Truncates
    body excerpt to 150 chars by default.

    Other shapes fall back to compact JSON, capped at 800 chars.
    """

    if _looks_like_email(input_data):
        return _format_email_input(input_data)
    return f"*Input:*\n```{_summarize_input(input_data)}```"


def _looks_like_email(input_data: dict[str, Any]) -> bool:
    return (
        "from_email" in input_data
        or "subject" in input_data
        or "snippet" in input_data
    )


def _format_email_input(input_data: dict[str, Any], *, summary_chars: int = 150) -> str:
    """Render an email payload as labeled markdown fields."""

    from_email = str(input_data.get("from_email") or "—")
    from_name = input_data.get("from_name")
    from_label = f"{from_name} <{from_email}>" if from_name else from_email
    to = input_data.get("to") or []
    if isinstance(to, list) and to:
        to_label = ", ".join(str(addr) for addr in to[:3])
        if len(to) > 3:
            to_label += f" (+{len(to) - 3} more)"
    else:
        to_label = "—"
    subject = str(input_data.get("subject") or "(no subject)")
    body = (
        input_data.get("body_excerpt")
        or input_data.get("snippet")
        or input_data.get("body")
        or ""
    )
    body = str(body).strip()
    if len(body) > summary_chars:
        body = body[: summary_chars - 1].rstrip() + "…"

    lines = [
        "*Email:*",
        f"• *From:* {from_label}",
        f"• *To:* {to_label}",
        f"• *Subject:* {subject}",
        f"• *Summary:* {body}" if body else "• *Summary:* (empty)",
    ]
    return "\n".join(lines)


def _summarize_input(input_data: dict[str, Any], *, max_chars: int = 800) -> str:
    """Short, deterministic JSON summary of an input dict for display."""

    serialized = json.dumps(input_data, indent=2, default=str, sort_keys=True)
    if len(serialized) > max_chars:
        return serialized[: max_chars - 4] + "\n…"
    return serialized


def _format_top_prediction(prior_predictions: dict[str, Any]) -> str:
    """Render the highest-confidence tier prediction as a one-liner.

    Avoids the JSON wall the prior layout produced. If multiple tiers ran,
    the human only needs to see the one closest to a decision; the full
    detail lives in the EvalRecord for audit.
    """

    if not prior_predictions:
        return ""
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for tier_id, pred in prior_predictions.items():
        if not isinstance(pred, dict):
            continue
        if pred.get("abstained", False):
            continue
        confidence = float(pred.get("confidence", 0.0) or 0.0)
        ranked.append((confidence, tier_id, pred))
    if not ranked:
        return "_No tier produced a confident answer._"
    ranked.sort(key=lambda item: item[0], reverse=True)
    confidence, tier_id, pred = ranked[0]
    output = pred.get("output", {})
    label_parts: list[str] = []
    for key in ("level1_classification", "label", "level2_intent"):
        value = output.get(key) if isinstance(output, dict) else None
        if value:
            label_parts.append(f"{key.split('_')[0]}=`{value}`")
    label_text = " ".join(label_parts) if label_parts else "(no labeled output)"
    return f"*Top prediction:* `{tier_id}` says {label_text} (confidence {confidence:.2f})"
