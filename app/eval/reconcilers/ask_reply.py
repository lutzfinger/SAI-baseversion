"""AskReplyReconciler — closes the Slack-ask loop.

Walks open Asks for a task, polls each ask's Slack thread for replies, and:

  1. Marks the Ask as ANSWERED with the parsed answer.
  2. For each linked EvalRecord, sets `reality` (source=SLACK_ASK) and
     `is_ground_truth=True` via `record_reality()`.

The reply parser is pluggable: the default extracts the first non-bot reply
text and stores it as `{"text": "..."}`. Tasks with structured options can
inject a parser that recognizes "yes/no/edit" or option-name matching.

The Slack reply polling assumes the conversations.replies API. The bot user
id is filtered out so the bot's own followups don't count as the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.eval.ask import Ask, AskStatus, AskStore
from app.eval.reconciler import (
    ReconciliationOutcome,
    ReconciliationResult,
)
from app.eval.record import EvalRecord, ObservedReality, RealitySource
from app.eval.storage import EvalRecordStore

if TYPE_CHECKING:
    from slack_sdk.web import WebClient

ReplyParser = Callable[[str], dict[str, Any]]


def default_reply_parser(text: str) -> dict[str, Any]:
    """Default parser: capture the raw reply text. Tasks can override."""

    return {"text": text.strip()}


class AskReplyReconciler:
    """Reconciler whose observation surface is replies to Slack Asks."""

    def __init__(
        self,
        *,
        task_id: str,
        client: WebClient,
        ask_store: AskStore,
        eval_store: EvalRecordStore,
        bot_user_id: str | None = None,
        reply_parser: ReplyParser = default_reply_parser,
        clock: Any = None,
    ) -> None:
        self.task_id = task_id
        self.client = client
        self.ask_store = ask_store
        self.eval_store = eval_store
        self.bot_user_id = bot_user_id
        self.reply_parser = reply_parser
        self._clock = clock or (lambda: datetime.now(UTC))

    def poll_open_asks(self) -> dict[str, int]:
        """Poll Slack for replies on every open Ask. Update Asks + linked records.

        Returns a count summary: {"answered": N, "still_open": M, "expired": K}.
        """

        counts = {"answered": 0, "still_open": 0, "expired": 0}
        now = self._clock()
        for ask in self.ask_store.open_asks(self.task_id):
            if ask.expires_at is not None and now > ask.expires_at:
                expired = ask.model_copy(
                    update={
                        "status": AskStatus.EXPIRED,
                        "answered_at": now,
                    }
                )
                self.ask_store.append(expired)
                counts["expired"] += 1
                continue

            answer = self._fetch_first_human_reply(ask)
            if answer is None:
                counts["still_open"] += 1
                continue

            answered = ask.model_copy(
                update={
                    "status": AskStatus.ANSWERED,
                    "answered_at": now,
                    "answered_by": answer.get("user"),
                    "answer": answer["parsed"],
                }
            )
            self.ask_store.append(answered)
            self._propagate_to_records(answered, observed_at=now)
            counts["answered"] += 1
        return counts

    # The Protocol-compatible per-record entry point. AskReplyReconciler is
    # used by RealityReconciliationRunner only for the SLACK_ASK case where
    # the linked Ask is already ANSWERED — meaning record propagation
    # happened during poll_open_asks. So reconcile_one looks for an answered
    # ask linked to this record and applies it; otherwise still_pending.
    def reconcile_one(
        self, record: EvalRecord, *, now: datetime | None = None
    ) -> ReconciliationResult:
        if record.ask_id is None:
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)
        latest_state = self.ask_store.latest_state(self.task_id)
        ask = latest_state.get(record.ask_id)
        if ask is None or ask.status != AskStatus.ANSWERED:
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)
        if ask.answer is None:
            return ReconciliationResult(outcome=ReconciliationOutcome.STILL_PENDING)
        return ReconciliationResult(
            outcome=ReconciliationOutcome.OBSERVED,
            reality=ObservedReality(
                label=ask.answer,
                source=RealitySource.SLACK_ASK,
                observed_at=ask.answered_at or (now or self._clock()),
                notes=f"slack ask {ask.ask_id} answered",
                raw_signal={"ask_id": ask.ask_id},
            ),
        )

    def _fetch_first_human_reply(self, ask: Ask) -> dict[str, Any] | None:
        if not ask.posted_to_thread_ts:
            return None
        try:
            response = self.client.conversations_replies(
                channel=ask.posted_to_channel,
                ts=ask.posted_to_thread_ts,
            )
        except Exception:  # pragma: no cover - any client error → still pending
            return None
        messages = response.get("messages") or []
        # First message is the parent (the ask itself); skip it.
        for msg in messages[1:]:
            user = msg.get("user")
            if not user or user == self.bot_user_id:
                continue
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            parsed = self.reply_parser(text)
            return {"user": user, "text": text, "parsed": parsed}
        return None

    def _propagate_to_records(self, ask: Ask, *, observed_at: datetime) -> None:
        if not ask.record_ids or ask.answer is None:
            return
        # We need to update each linked EvalRecord. Since the store is JSONL
        # append-only, we read the latest state, find by record_id, and
        # append the updated record.
        latest_records = {
            rec.record_id: rec
            for rec in self.eval_store.read_all(self.task_id)
        }
        for record_id in ask.record_ids:
            record = latest_records.get(record_id)
            if record is None or record.is_ground_truth:
                continue
            updated = record.model_copy(deep=True)
            updated.record_reality(
                ObservedReality(
                    label=ask.answer,
                    source=RealitySource.SLACK_ASK,
                    observed_at=ask.answered_at or observed_at,
                    notes=f"slack ask {ask.ask_id} answered",
                    raw_signal={"ask_id": ask.ask_id},
                )
            )
            self.eval_store.append(updated)
