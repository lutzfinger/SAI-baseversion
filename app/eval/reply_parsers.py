"""Reusable Slack-reply parser factories.

The eval-feedback loop expects each task to provide a `reply_parser:
ReplyParser = Callable[[str], dict[str, Any]]` that turns the human's
reply text into a structured answer dict. The reply parser's contract:

    Returns a dict with at least:
      - "valid": bool                  — required
    On valid replies (`valid=True`), additional keys carry the answer
    (`level1_classification`, `label`, `text`, etc).
    On invalid replies (`valid=False`), include:
      - "expected_options": list[str]  — what the user could try next
      - "reason": str                  — short explanation
      - "text": str                    — what the user typed

The AskReplyReconciler honors this contract — on `valid=False` it posts
a clarifying reply to the Slack thread and leaves the ask OPEN.

This module ships TASK-AGNOSTIC parser FACTORIES. The actual list of
options for any given task lives in private overlay code (because the
options ARE the task's data — your L1 taxonomy, your travel preference
labels, etc). Wire them via:

    from app.eval.reply_parsers import option_matching_parser
    parser = option_matching_parser(
        options=["customers", "personal", ...],
        output_field="level1_classification",
        second_field="level2_intent",
        second_field_options=["informational", "action_required"],
    )
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ReplyParser = Callable[[str], dict[str, Any]]

DEFAULT_SKIP_KEYWORDS: tuple[str, ...] = ("skip", "drop", "no", "ignore", "n/a")


def option_matching_parser(
    *,
    options: list[str],
    output_field: str = "label",
    skip_keywords: tuple[str, ...] = DEFAULT_SKIP_KEYWORDS,
    case_sensitive: bool = False,
    second_field: str | None = None,
    second_field_options: list[str] | None = None,
) -> ReplyParser:
    """Build a ReplyParser that matches replies against a fixed option list.

    Args:
        options: valid first-token answers (e.g. L1 bucket names).
        output_field: dict key for the matched first option.
        skip_keywords: replies that mark the record as skipped (still valid).
        case_sensitive: if False (default), normalize input + options to lower.
        second_field: optional dict key for the second token (e.g.
            "level2_intent"). If set, parses a second whitespace-separated
            token and looks it up in `second_field_options`.
        second_field_options: valid second-token answers. Required when
            `second_field` is set.

    Returns a parser callable conforming to the ReplyParser contract.
    """

    if second_field is not None and not second_field_options:
        raise ValueError(
            "second_field_options is required when second_field is set"
        )
    if not options:
        raise ValueError("options must be non-empty")

    def _normalize(text: str) -> str:
        return text.strip() if case_sensitive else text.strip().lower()

    canonical_first = {_normalize(opt): opt for opt in options}
    skip_set = {_normalize(kw) for kw in skip_keywords}
    canonical_second: dict[str, str] = {
        _normalize(opt): opt for opt in (second_field_options or [])
    }

    def parser(text: str) -> dict[str, Any]:
        raw = text or ""
        normalized = _normalize(raw)

        if not normalized:
            return {
                "text": raw,
                "valid": False,
                "expected_options": list(options),
                "reason": "empty reply",
            }

        # "skip"/"drop"/etc — explicit user choice to drop from training,
        # still considered valid (the reconciler will confirm + dropping
        # the record is also a recorded outcome).
        if normalized in skip_set:
            return {
                "skipped": True,
                "text": raw.strip(),
                "valid": True,
                "reason": "human asked to skip / not applicable",
            }

        tokens = normalized.replace(",", " ").split()
        first_token = tokens[0]
        if first_token not in canonical_first:
            return {
                "text": raw.strip(),
                "valid": False,
                "expected_options": list(options),
                "reason": f"`{first_token}` is not a recognized {output_field}",
            }

        answer: dict[str, Any] = {
            output_field: canonical_first[first_token],
            "valid": True,
        }

        if second_field and len(tokens) > 1:
            second_token = tokens[1]
            if second_token in canonical_second:
                answer[second_field] = canonical_second[second_token]
            else:
                # First-token was valid; trailing token wasn't recognized
                # as a valid second-field value. Don't push back — accept
                # the primary answer, note the ignored extra.
                answer["note"] = (
                    f"trailing token `{second_token}` not recognized as "
                    f"{second_field}; ignored"
                )
        return answer

    return parser


def permissive_text_parser(text: str) -> dict[str, Any]:
    """Trivial parser: capture raw text verbatim, always valid.

    Use for tasks where any free-form reply is meaningful (e.g. preference
    refinement approvals where the human types yes / no / "rephrase as
    ...").
    """

    return {"text": text.strip(), "valid": True}
