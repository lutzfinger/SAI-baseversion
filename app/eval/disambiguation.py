"""Operator-facing disambiguation for fuzzy add_eval references.

When the operator types something like "dinika should be customers", the
``gmail_message_resolver`` returns 0, 1, or many candidate messages. This
module converts that into the right operator-facing action:

  - **0 matches** — apologise; suggest a more specific reference. No
    proposal staged.
  - **1 match**  — show the candidate; ask the operator to react ✅ to
    apply or ❌ to cancel. Proposal can be staged immediately by the
    caller using ``candidate.message_id``.
  - **2+ matches** — list candidates 1..N; ask the operator to react
    with the corresponding number emoji to pick one. Caller persists
    the candidates via ``write_pending_choice`` and resolves on the
    next number-emoji reaction.

This is the missing middle layer between the resolver (data) and the
slack_bot (UX). It owns:

  * The decision tree (0/1/many → operator message + next action)
  * The number-emoji ↔ candidate-index mapping
  * The pending-choice persistence (caller still owns Slack I/O)

Tests at ``tests/test_disambiguation.py`` cover the decision tree, the
emoji mapping, and the round-trip through pending-choice persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional

from app.eval.gmail_message_resolver import ResolveCandidate, ResolveResult

# How many candidates to surface in the "multiple" message. Past this we
# still ask the operator to be more specific — surfacing 20 candidates
# in Slack is operator-hostile.
MAX_CANDIDATES_SHOWN: int = 5


# Slack delivers reaction events with the EMOJI NAME (not the glyph).
# Number emoji names are :one:, :two:, ..., :keycap_ten:. Slack also
# accepts the digit aliases for some (":1:" is not standard, but Slack
# may also normalise to "1"). We accept the canonical name + the digit.
_NUMBER_EMOJI_TO_INDEX: dict[str, int] = {
    "one": 0, "1": 0,
    "two": 1, "2": 1,
    "three": 2, "3": 2,
    "four": 3, "4": 3,
    "five": 4, "5": 4,
    "six": 5, "6": 5,
    "seven": 6, "7": 6,
    "eight": 7, "8": 7,
    "nine": 8, "9": 8,
    "keycap_ten": 9, "10": 9,
}

# What we render next to each candidate in the "multiple" list. Slack
# renders these as the keycap-digit glyphs.
_NUMBER_EMOJI_DISPLAY: list[str] = [
    ":one:", ":two:", ":three:", ":four:", ":five:",
    ":six:", ":seven:", ":eight:", ":nine:", ":keycap_ten:",
]


@dataclass(frozen=True)
class DisambiguationOutcome:
    """Structured outcome of ``classify_outcome``."""

    kind: Literal["none", "unique", "multiple"]
    candidates: list[ResolveCandidate]
    """- none: empty
       - unique: 1 candidate
       - multiple: up to MAX_CANDIDATES_SHOWN candidates
    """
    operator_message: str
    """The Slack-ready message text the bot should post."""


def classify_outcome(
    *,
    target_pattern: str,
    bucket: str,
    resolve_result: ResolveResult,
    max_candidates_shown: int = MAX_CANDIDATES_SHOWN,
) -> DisambiguationOutcome:
    """Map a resolver outcome to the right operator action.

    The ``target_pattern`` and ``bucket`` are echoed back into the
    operator message so the operator sees exactly what the bot
    interpreted.
    """

    # Resolver itself failed (auth, network, etc.) — different shape
    # from "0 matches". Surface the underlying error so the operator
    # can act on it (top up token, refresh OAuth, etc.).
    if resolve_result.error:
        return DisambiguationOutcome(
            kind="none",
            candidates=[],
            operator_message=(
                f"Couldn't search Gmail for `{target_pattern}` — "
                f"_{resolve_result.error}_"
            ),
        )

    if resolve_result.count == 0:
        return DisambiguationOutcome(
            kind="none",
            candidates=[],
            operator_message=(
                f"Couldn't find any mail matching `{target_pattern}` in "
                f"the last 30 days. Try a fuller name, an email address, "
                f"or a domain — e.g. "
                f"`alex@example.com should be {bucket}`."
            ),
        )

    if resolve_result.is_unique:
        only = resolve_result.candidates[0]
        return DisambiguationOutcome(
            kind="unique",
            candidates=[only],
            operator_message=(
                f"Found one match for `{target_pattern}`:\n"
                f"> {only.short_summary()}\n"
                f"I'll mark it `L1/{bucket}` for the eval set. "
                f"React ✅ to apply or ❌ to cancel."
            ),
        )

    # 2+ matches.
    shown = resolve_result.candidates[:max_candidates_shown]
    lines = [
        f"Found {resolve_result.count} matches for `{target_pattern}`. "
        f"React with the number of the one you mean (or ❌ to cancel):"
    ]
    for idx, cand in enumerate(shown):
        lines.append(f"{_NUMBER_EMOJI_DISPLAY[idx]}  {cand.short_summary()}")
    if resolve_result.count > max_candidates_shown:
        lines.append(
            f"_…and {resolve_result.count - max_candidates_shown} more — "
            f"if it's not in this list, narrow the search "
            f"(use the email address or a fuller name)._"
        )
    lines.append(
        f"_Whichever you pick, I'll mark it `L1/{bucket}` for the eval "
        f"set after you confirm._"
    )
    return DisambiguationOutcome(
        kind="multiple",
        candidates=shown,
        operator_message="\n".join(lines),
    )


# ─── pending-choice persistence ───────────────────────────────────────


def number_emoji_to_index(reaction_name: str) -> Optional[int]:
    """Translate a Slack reaction emoji name into a 0-based index.

    Returns ``None`` if the emoji is not one of the digit emojis.
    Caller filters those out so the bot stays silent on unrelated
    reactions per principle #9 pattern-bound.
    """

    return _NUMBER_EMOJI_TO_INDEX.get(reaction_name.lower())


def write_pending_choice(
    *,
    choices_dir: Path,
    message_ts: str,
    channel: str,
    candidates: list[ResolveCandidate],
    bucket: str,
    proposed_by: str,
    source_text: str,
) -> Path:
    """Persist the multi-candidate state so a later number-emoji
    reaction can be resolved into a concrete proposal.

    Stored as JSON keyed by the bot's "pick one" message_ts. Written
    atomically (write-then-rename) so a daemon crash mid-write doesn't
    leave a half-file the lookup will choke on.
    """

    if not message_ts:
        raise ValueError("message_ts required for pending choice")
    choices_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "message_ts": message_ts,
        "channel": channel,
        "bucket": bucket,
        "proposed_by": proposed_by,
        "source_text": source_text,
        "indexed_at": datetime.now(UTC).isoformat(),
        "candidates": [
            {
                "message_id": c.message_id,
                "thread_id": c.thread_id,
                "from_email": c.from_email,
                "from_name": c.from_name,
                "subject": c.subject,
                "snippet": c.snippet,
                "received_at_iso": c.received_at_iso,
            }
            for c in candidates
        ],
    }
    safe_ts = message_ts.replace(".", "_")
    out = choices_dir / f"choice_{safe_ts}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(out)
    return out


def lookup_pending_choice(
    *, choices_dir: Path, message_ts: str,
) -> Optional[dict[str, Any]]:
    """Return the persisted pending-choice payload or ``None``."""

    if not message_ts:
        return None
    safe_ts = message_ts.replace(".", "_")
    path = choices_dir / f"choice_{safe_ts}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        # Corrupt file (mid-write crash before the rename, etc.) — drop
        # so subsequent lookups don't keep tripping. Better to lose a
        # pending choice than to wedge the bot.
        path.unlink(missing_ok=True)
        return None


def drop_pending_choice(*, choices_dir: Path, message_ts: str) -> None:
    """Remove a pending-choice file after resolution or cancellation."""

    if not message_ts:
        return
    safe_ts = message_ts.replace(".", "_")
    path = choices_dir / f"choice_{safe_ts}.json"
    path.unlink(missing_ok=True)


def candidate_from_pending(
    payload: dict[str, Any], index: int,
) -> Optional[ResolveCandidate]:
    """Re-hydrate a ``ResolveCandidate`` from a stored choice payload.

    Returns ``None`` if `index` is out of range — the caller's signal
    to post "that number wasn't on the list" rather than crash.
    """

    candidates = payload.get("candidates") or []
    if index < 0 or index >= len(candidates):
        return None
    raw = candidates[index]
    return ResolveCandidate(
        message_id=raw["message_id"],
        thread_id=raw.get("thread_id") or raw["message_id"],
        from_email=raw["from_email"],
        from_name=raw.get("from_name"),
        subject=raw.get("subject") or "(no subject)",
        snippet=raw.get("snippet") or "",
        received_at_iso=raw.get("received_at_iso"),
    )
