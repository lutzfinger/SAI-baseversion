"""AskReplyReconciler — closes the Slack-ask loop.

Walks open Asks for a task, polls each ask's Slack thread for replies, and:

  1. Marks the Ask as ANSWERED with the parsed answer.
  2. For each linked EvalRecord, sets `reality` (source=SLACK_ASK) and
     `is_ground_truth=True` via `record_reality()`.
  3. Posts a confirmation reply in the thread so the human knows their
     answer was received and applied.
  4. If the parser flags the answer as invalid (`valid=false`), posts a
     clarification reply ("didn't recognize X, try one of: ...") and
     leaves the ask OPEN so the next reply gets another shot.

The reply parser is pluggable. The default extracts the raw text and is
permissive (always valid). Task-specific parsers (e.g. the L1-aware one
for email_classification) validate against expected options and set
`valid=False` when the answer doesn't match.

The Slack reply polling assumes the conversations.replies API. The bot user
id is filtered out so the bot's own followups don't count as the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
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
    """Default parser: capture the raw reply text. Always valid (permissive)."""

    return {"text": text.strip(), "valid": True}


def _summarize_answer(answer: dict[str, Any]) -> str:
    """One-line human-readable rendering of a parsed answer for confirmation."""

    if not isinstance(answer, dict):
        return f"answer: `{answer}`"
    parts: list[str] = []
    for key in ("level1_classification", "level2_intent", "label", "text"):
        value = answer.get(key)
        if value:
            parts.append(f"{key.split('_')[0]}=`{value}`")
            break  # show the first meaningful field; full detail in record
    if not parts:
        parts.append(f"`{answer}`")
    return " ".join(parts)


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

        On valid answers, marks the Ask ANSWERED, propagates ground truth to
        linked records, and posts a confirmation reply in the thread.
        On invalid answers (parser sets `valid=False`), posts a clarification
        reply and leaves the Ask OPEN.

        Returns: {"answered": N, "still_open": M, "expired": K, "clarified": C}
        """

        counts = {"answered": 0, "still_open": 0, "expired": 0, "clarified": 0}
        now = self._clock()
        # Track which thread_ts we already wrote a clarification into THIS run,
        # so a single human reply that's invalid doesn't get clarified twice
        # if poll_open_asks is called repeatedly within the same minute.
        clarified_threads_this_run: set[str] = set()

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

            parsed = answer["parsed"]
            valid = parsed.get("valid", True)

            if not valid:
                thread = ask.posted_to_thread_ts or ""
                if thread and thread not in clarified_threads_this_run:
                    self._post_clarification(ask, parsed=parsed)
                    clarified_threads_this_run.add(thread)
                counts["clarified"] += 1
                counts["still_open"] += 1
                continue

            answered = ask.model_copy(
                update={
                    "status": AskStatus.ANSWERED,
                    "answered_at": now,
                    "answered_by": answer.get("user"),
                    "answer": parsed,
                }
            )
            self.ask_store.append(answered)
            self._propagate_to_records(answered, observed_at=now)
            self._post_confirmation(answered)
            counts["answered"] += 1
        return counts

    def _post_confirmation(self, ask: Ask) -> None:
        """Post a `Got it: ...` reply in the ask's thread."""

        if not ask.posted_to_thread_ts or ask.answer is None:
            return
        summary = _summarize_answer(ask.answer)
        text = (
            f":white_check_mark: Got it — {summary}. "
            f"EvalRecord(s) updated."
        )
        with suppress(Exception):  # pragma: no cover - any post failure is non-fatal
            self.client.chat_postMessage(
                channel=ask.posted_to_channel,
                thread_ts=ask.posted_to_thread_ts,
                text=text,
            )

    def _post_clarification(self, ask: Ask, *, parsed: dict[str, Any]) -> None:
        """Post a `didn't recognize X, try one of: ...` reply in the thread."""

        if not ask.posted_to_thread_ts:
            return
        offered = parsed.get("text") or parsed.get("raw") or "(no text)"
        options = parsed.get("expected_options") or ask.options or []
        options_part = ""
        if options:
            options_part = " Try one of: " + ", ".join(f"`{o}`" for o in options[:12])
        text = (
            f":grey_question: I didn't recognize `{offered}` as a valid answer."
            f"{options_part}"
        )
        with suppress(Exception):  # pragma: no cover - any post failure is non-fatal
            self.client.chat_postMessage(
                channel=ask.posted_to_channel,
                thread_ts=ask.posted_to_thread_ts,
                text=text,
            )

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
        """Update every EvalRecord linked to this answered Ask.

        Records are linked two ways: explicitly via `ask.record_ids` (the
        AskOrchestrator populates this) AND implicitly via the record's own
        `ask_id` field (the runner sets this when HumanTier creates the
        ask). We honor both so neither call site has to track the linkage
        separately.
        """

        if ask.answer is None:
            return

        all_records = self.eval_store.read_all(self.task_id)
        # Latest line per record_id wins on fold.
        latest_by_record_id: dict[str, EvalRecord] = {}
        for record in all_records:
            latest_by_record_id[record.record_id] = record

        # Collect every record that should be updated:
        #   - explicit record_ids on the ask
        #   - records whose own ask_id field matches this ask
        targets: dict[str, EvalRecord] = {}
        for record_id in ask.record_ids:
            record = latest_by_record_id.get(record_id)
            if record is not None:
                targets[record.record_id] = record
        for record in latest_by_record_id.values():
            if record.ask_id == ask.ask_id:
                targets[record.record_id] = record

        for record in targets.values():
            if record.is_ground_truth:
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
