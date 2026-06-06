"""Tests for the markdown-strip output guard added 2026-05-26.

Per SAI #6a (schema enforcement at every boundary) the receipt-
collector daemon's email replies must reach Gmail web + Superhuman as
plain text. Before this guard, the May 21 "idea: weekly content
planner" reply leaked `**Subject:**`, `## What this could look like`,
`---`, and `[text](url)` markup literally. These tests pin the
behavior so it never regresses.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib import email_format


# ─── unit tests for the stripper ──────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("**bold**", "bold"),
        ("__bold__", "bold"),
        ("*italic*", "italic"),
        ("_italic_", "italic"),
        ("## Header", "Header"),
        ("### Sub-Header", "Sub-Header"),
        ("# Big Header", "Big Header"),
        ("---", ""),
        ("***", ""),
        ("___", ""),
        ("`inline code`", "inline code"),
        ("Use `my_var` here", "Use my_var here"),
        ("[docs](https://example.com)", "docs (https://example.com)"),
        ("[Forbes](https://example.com)", "Forbes (https://example.com)"),
        ("<https://example.com>", "https://example.com"),
        ("Plain text", "Plain text"),
        ("", ""),
    ],
)
def test_strip_markdown_unit_cases(raw: str, want: str) -> None:
    assert email_format.strip_markdown_for_plaintext_email(raw) == want


def test_strip_markdown_unwraps_link_with_same_text_as_url() -> None:
    out = email_format.strip_markdown_for_plaintext_email(
        "[https://example.com](https://example.com)"
    )
    assert out == "https://example.com"


def test_strip_markdown_bullet_normalisation() -> None:
    out = email_format.strip_markdown_for_plaintext_email("* one\n* two\n+ three")
    assert out == "- one\n- two\n- three"


def test_strip_markdown_fenced_code_block() -> None:
    out = email_format.strip_markdown_for_plaintext_email(
        textwrap.dedent(
            """
            Here is code:
            ```python
            print("hi")
            ```
            done.
            """
        ).strip()
    )
    assert "```" not in out
    assert 'print("hi")' in out


def test_strip_markdown_collapses_runs_of_blank_lines() -> None:
    out = email_format.strip_markdown_for_plaintext_email("A\n\n\n\n\nB")
    assert out == "A\n\nB"


def test_strip_markdown_is_idempotent_on_plain_text() -> None:
    plain = "Hello operator — the trip is confirmed.\n\nReply 'y' to approve."
    once = email_format.strip_markdown_for_plaintext_email(plain)
    twice = email_format.strip_markdown_for_plaintext_email(once)
    assert once == plain
    assert twice == plain


def test_strip_markdown_handles_none_and_empty() -> None:
    assert email_format.strip_markdown_for_plaintext_email("") == ""


# ─── regression for the actual May 21 leaked reply ────────────────────


_MAY_21_LEAKED_BODY = textwrap.dedent(
    """\
    **Subject: RE: idea: weekly content planner**

    Got it — you want Monday automation that checks your calendar load.

    ---

    ## What this could look like:

    **Trigger:** Every Monday at 8am (scheduled)

    **What it reads:**
    - Your Google Calendar for the coming week
    - Optional: your past 4–6 LinkedIn posts

    **What it produces:**
    - A Slack/email summary

    ---

    **This is a proposal.**
    """
)


def test_may_21_workflow_suggestion_reply_has_no_markdown_markers() -> None:
    out = email_format.workflow_suggestion_reply(_MAY_21_LEAKED_BODY)
    assert "**" not in out
    assert "##" not in out
    assert "---" not in out
    assert "[" not in out  # no leftover link markup
    # The human-readable content must still be present.
    assert "Subject: RE: idea: weekly content planner" in out
    assert "Trigger:" in out
    assert "Google Calendar" in out


def test_general_assistant_reply_strips_markdown() -> None:
    out = email_format.general_assistant_reply(
        "**Here is** a thought.\n\n## Header\n\n[Reference](https://example.com)"
    )
    assert "**" not in out
    # The stripper only unwraps `## ` at line-start (the CommonMark
    # spec). An inline `##` in the middle of prose is not a header
    # marker and is left intact — but this test puts it at line-start.
    assert "##" not in out
    assert "Here is a thought." in out
    assert "Reference (https://example.com)" in out


def test_ad_hoc_proposal_reply_strips_markdown() -> None:
    out = email_format.ad_hoc_proposal_reply(
        "TLDR: I don't have this as an approved workflow.\n\n"
        "**STEPS:**\n"
        "1. Search Gmail for `cherry` agreements.\n"
        "2. Search [Drive](https://drive.google.com).\n"
        "\n"
        "Approve y/n"
    )
    assert "**" not in out
    assert "`" not in out
    assert "TLDR" in out
    assert "STEPS:" in out
    assert "Drive (https://drive.google.com)" in out
    assert "Approve y/n" in out
