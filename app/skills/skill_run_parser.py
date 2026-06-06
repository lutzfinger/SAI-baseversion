"""Parse natural-language "run <skill>" commands from Slack DMs / emails.

The slack bot's `_handle_top_level_operator_message` calls into this
module as a Tier 0 check (cheap, deterministic) BEFORE falling through
to the LangChain agent.

Add a new pattern by extending `_PARSERS` below. Each parser returns
`SkillRunInvocation` on match, `None` on no-match.

Example operator messages this module recognises:

  "run student participation check for C-Suites May 2026 INSEAD, all sessions,
   https://docs.google.com/spreadsheets/d/.../edit"

  "student participation check, folder=AI Strategy May 2026 INSEAD,
   dates=2026-05-08:2026-05-15, sheet=https://docs.google.com/..."

Per #16e — explicit triggers only. If a message looks like it might be a
"run <skill>" command but the parser can't extract required params, the
parser returns SkillRunInvocation with `error_reason` set so the bot can
post a structured error rather than silently falling through to the LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


_GSHEET_URL_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-?=&#./]+"
)
_DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DATE_RANGE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\s*(?::|to|-|–|—|through)\s*(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)


@dataclass
class SkillRunInvocation:
    """One parsed 'run <skill>' command, ready to invoke."""
    workflow_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    error_reason: Optional[str] = None       # if not None, parse partially failed
    matched_phrase: str = ""                 # the phrase that triggered the parser


def parse_run_student_participation(text: str) -> Optional[SkillRunInvocation]:
    """Recognise:
      'run student participation check ...'
      'student participation check ...'
      'check student participation ...'
    and extract folder, date range, sheet URL.
    """
    t = text.strip()
    tl = t.lower()
    trigger_phrases = (
        "run student participation check",
        "student participation check",
        "check student participation",
        "participation check",
    )
    matched = next((p for p in trigger_phrases if p in tl), None)
    if matched is None:
        return None

    # Strip the trigger phrase to get the remainder
    idx = tl.find(matched)
    remainder = t[idx + len(matched):].lstrip(" ,:-—")

    # --- Sheet URL ---
    sheet_match = _GSHEET_URL_RE.search(remainder)
    sheet_url = sheet_match.group(0) if sheet_match else None

    # --- Date range ---
    date_range: Optional[str] = None
    range_match = _DATE_RANGE_RE.search(remainder)
    if range_match:
        date_range = f"{range_match.group(1)}:{range_match.group(2)}"
    else:
        # Single date
        single_dates = _DATE_ISO_RE.findall(remainder)
        if len(single_dates) == 1:
            date_range = f"{single_dates[0]}:{single_dates[0]}"
        elif len(single_dates) >= 2:
            date_range = f"{single_dates[0]}:{single_dates[-1]}"
    # Relative dates ('all sessions', 'last week', 'May 2026') are
    # NOT resolved here — the agent layer above should resolve them
    # before invoking the skill. The parser passes the raw phrase
    # through so downstream can render an error if needed.

    # --- Folder name ---
    # Heuristic: everything up to the first comma OR the date OR the URL,
    # minus the leading "for ".
    folder = remainder
    for cutoff in (sheet_url or "",
                   range_match.group(0) if range_match else "",
                   ", all sessions", "all sessions"):
        if cutoff and cutoff in folder:
            folder = folder.split(cutoff)[0]
    folder = folder.strip(" ,:-—").removeprefix("for ").removeprefix("for: ")
    # Strip trailing date / range markers
    folder = re.sub(r"\b(?:between|from)\b.*$", "", folder, flags=re.IGNORECASE).strip(" ,:-—")
    if not folder:
        folder = None

    # --- "all sessions" sentinel ---
    all_sessions = "all sessions" in tl

    error = None
    if not folder:
        error = "missing folder name (e.g. 'for C-Suites May 2026 INSEAD')"
    if not sheet_url:
        error = (error + "; " if error else "") + "missing Google Sheet URL"

    return SkillRunInvocation(
        workflow_id="student-participation-check",
        inputs={
            "folder": folder or "",
            "date_range": date_range or ("all" if all_sessions else ""),
            "sheet_url": sheet_url or "",
            "all_sessions": all_sessions,
        },
        error_reason=error,
        matched_phrase=matched,
    )


# Ordered list of parsers. First non-None wins.
_PARSERS: list[Callable[[str], Optional[SkillRunInvocation]]] = [
    parse_run_student_participation,
    # Future: parse_run_<other-skill>,
]


def parse_skill_run(text: str) -> Optional[SkillRunInvocation]:
    """Try each registered parser in order. Returns first match or None."""
    for parser in _PARSERS:
        invocation = parser(text)
        if invocation is not None:
            return invocation
    return None
