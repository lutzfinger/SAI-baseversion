"""email_format — turn machine-y status messages into operator-friendly
email replies.

The previous bot replies dumped raw subprocess stdout / exit codes /
file paths into the email body. The operator called
this "non human readable, way too badly written."

This module rewrites bot output in a conversational tone, with the
dense technical details collapsed into an optional `Details` section
the operator can scroll past.

Public API (all return a string suitable for direct email body):

  plan_staged(plan, agent_summary) -> str
      Format the "I'll do these things for you" reply right after a
      plan is staged.

  collect_phase_done(slug, customer, start, end, steps_log,
                      staged_plan_path) -> str
      Format the "here's what I found, ready for your approval" reply.

  collect_phase_failed(slug, steps_log, last_error) -> str
      Format the "something went wrong" reply — keeps the same friendly
      tone, surfaces what to try.

  invoice_done(invoice, downloads_dir) -> str
      Format the "all done, here's the result" reply after approval.

  general_assistant_reply(text) -> str
      Wrap a general-assistant reply with a tiny friendly opener
      (no boilerplate, just a clean conversational message).

  workflow_suggestion_reply(text) -> str
      Pass-through for the model-drafted case-(b) "no approved workflow"
      template (one-line headline + ~100-word explanation + copy-paste
      Claude Code prompt).

  ad_hoc_proposal_reply(text) -> str
      Pass-through for the model-drafted case-(c) "TLDR + STEPS +
      Approve y/n" proposal.

  eval_acknowledged() -> str
      Brief acknowledgment for EVAL_FEEDBACK route.

  clarification_needed(question) -> str
      For when the bot needs more info — make it sound like a
      colleague, not a form rejection.

  strip_markdown_for_plaintext_email(text) -> str
      Output guard (per SAI #6a). Gmail web + Superhuman render
      plaintext bodies literally, so every reply that reaches this
      module gets `**bold**`, `## headers`, fenced code, link markup,
      etc. unwrapped to readable plain text BEFORE it leaves.
"""
from __future__ import annotations

import re
import textwrap
from typing import Optional


# ─── markdown → plaintext output guard ─────────────────────────────────
#
# the operator's clients (Gmail web, Superhuman) render plaintext email bodies
# literally — so `**Subject**`, `## Header`, `---`, and `[text](url)`
# show with the markup. Per SAI #6a (output guards on every boundary)
# we unwrap them before send. This is intentionally conservative: we
# strip the *markers* and keep the *text*. No HTML synthesis, no
# Unicode substitution, no line reflow.


_FENCED_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+\-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADER_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BOLD_DOUBLE_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_DOUBLE_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_SINGLE_STAR = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_ITALIC_SINGLE_UNDERSCORE = re.compile(r"(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?!_)", re.DOTALL)
_HORIZONTAL_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}\s*$", re.MULTILINE)
_BULLET_PREFIX = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_INLINE_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*(<?[^)\s]+>?)(?:\s+\"[^\"]*\")?\s*\)")
_HTML_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _unwrap_link(match: re.Match[str]) -> str:
    text = match.group(1).strip()
    href = match.group(2).strip().strip("<>")
    if not href or href == text:
        return text
    return f"{text} ({href})"


def strip_markdown_for_plaintext_email(text: str) -> str:
    """Unwrap markdown so the body reads cleanly in Gmail/Superhuman.

    Idempotent on already-plain text. Preserves paragraph structure,
    list-item dashes, and the order of words; only removes the *markup*.
    """
    if not text:
        return text

    out = text.replace("\r\n", "\n").replace("\r", "\n")

    out = _FENCED_CODE_BLOCK.sub(lambda m: m.group(1).rstrip(), out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _AUTOLINK.sub(r"\1", out)
    out = _INLINE_LINK.sub(_unwrap_link, out)
    out = _HORIZONTAL_RULE.sub("", out)
    out = _HEADER_LINE.sub(r"\1", out)
    out = _BOLD_DOUBLE_STAR.sub(r"\1", out)
    out = _BOLD_DOUBLE_UNDERSCORE.sub(r"\1", out)
    out = _ITALIC_SINGLE_STAR.sub(r"\1", out)
    out = _ITALIC_SINGLE_UNDERSCORE.sub(r"\1", out)
    out = _BULLET_PREFIX.sub(r"\1- ", out)
    out = _HTML_BR.sub("\n", out)

    # Collapse the now-empty lines left behind by stripped HR rules,
    # but never collapse a genuine paragraph break to zero.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + ("\n" if text.endswith("\n") else "")


def _friendly_dates(start: str, end: str) -> str:
    """Convert ISO dates to natural-language dates."""
    from datetime import date
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except Exception:
        return f"{start} to {end}"
    months_en = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    if s.year == e.year and s.month == e.month:
        return f"{months_en[s.month-1]} {s.day}–{e.day}, {s.year}"
    if s.year == e.year:
        return f"{months_en[s.month-1]} {s.day} – {months_en[e.month-1]} {e.day}, {s.year}"
    return f"{months_en[s.month-1]} {s.day}, {s.year} – {months_en[e.month-1]} {e.day}, {e.year}"


def plan_staged(plan: dict, agent_summary: str = "") -> str:
    """Friendly reply right after the agent stages a plan."""
    cust = plan.get("customer", {}).get("DisplayName", "the customer")
    currency = plan.get("invoice_currency", "USD")
    start = plan.get("trip_start", "?")
    end = plan.get("trip_end", "?")
    window = _friendly_dates(start, end)
    scope = plan.get("scope_categories") or []
    scope_part = "all categories" if not scope else ", ".join(scope)

    out = [
        f"Got it — I'll pull together your {cust} trip receipts from "
        f"{window}, billing in {currency}. Scope: {scope_part}.",
        "",
        "Starting the collect phase now. I'll reply with a review "
        "summary when it's done so you can approve or adjust before "
        "any QuickBooks writes happen.",
    ]
    return "\n".join(out)


def collect_phase_done(
    slug: str, customer: str, start: str, end: str,
    steps_log: list[dict],
    staged_plan_path: Optional[str] = None,
    advisory_failures: Optional[list[str]] = None,
) -> str:
    """Friendly review summary, ready for approval. `steps_log` is a
    list of {name, exit_code, ok, criticality, summary_line}.
    """
    window = _friendly_dates(start, end)
    advisory_failures = advisory_failures or []

    # Headline summary
    out = [
        f"Here's what I found for the {customer} trip ({window}).",
        "",
    ]

    # Find the scan-cards summary if present
    found_purchases = None
    for s in steps_log:
        if s.get("name") == "scan-cards" and s.get("summary_line"):
            found_purchases = s["summary_line"]
            break
    if found_purchases:
        out.append(f"• {found_purchases}")

    # Mention any advisory failures honestly but briefly
    if advisory_failures:
        out.append("")
        out.append(
            "A couple of optional steps didn't complete — these don't "
            "block the invoice but you may want to re-run them manually:"
        )
        for f in advisory_failures:
            out.append(f"  – {f}")

    out.append("")
    out.append(
        "**Reply YES** to create the invoice in QuickBooks "
        "(I will NOT send it; it stays in draft for your review). "
        "**Reply NO** to drop the plan."
    )

    # Tech details collapsed at the bottom
    out.append("")
    out.append("---")
    out.append("_Details (skim or skip):_")
    out.append(f"_• Trip slug: `{slug}`_")
    if staged_plan_path:
        out.append(f"_• Plan staged: `{staged_plan_path}`_")
    out.append("_• Steps:_")
    for s in steps_log:
        emoji = "✓" if s.get("ok") else "✗"
        out.append(f"_  {emoji} {s['name']} (exit {s.get('exit_code', '?')})_")
    return "\n".join(out)


def collect_phase_failed(slug: str, steps_log: list[dict],
                          last_error: str = "") -> str:
    """When a blocking step in the collect phase failed."""
    out = [
        "I ran into a problem during the collect phase and stopped "
        "before staging anything for your approval.",
        "",
    ]
    # Highlight the failure
    failed = [s for s in steps_log if not s.get("ok")]
    if failed:
        last = failed[-1]
        out.append(f"What failed: **{last['name']}** "
                   f"(exit code {last.get('exit_code', '?')}).")
        if last_error:
            # Pick the most informative line from the traceback
            err_summary = _extract_error_summary(last_error)
            if err_summary:
                out.append(f"Underlying error: `{err_summary}`")
        out.append("")
        out.append("Reply with the corrected details or what you'd "
                   "like me to try differently.")
    out.append("")
    out.append("---")
    out.append("_Details:_")
    out.append(f"_• Trip slug: `{slug}`_")
    for s in steps_log:
        emoji = "✓" if s.get("ok") else "✗"
        out.append(f"_  {emoji} {s['name']} (exit {s.get('exit_code', '?')})_")
    return "\n".join(out)


def _extract_error_summary(traceback_text: str) -> str:
    """Pull the most informative line from a Python traceback."""
    if not traceback_text:
        return ""
    lines = [ln.strip() for ln in traceback_text.splitlines() if ln.strip()]
    # Look for the last "ErrorClass: message" line
    for ln in reversed(lines):
        if ":" in ln and not ln.startswith("File ") and not ln.startswith("/"):
            # Likely an exception summary line
            if any(kw in ln for kw in (
                "Error:", "Exception:", "PermissionError",
                "FileNotFoundError", "TimeoutExpired",
                "ConnectionError", "RuntimeError",
            )):
                return ln[:200]
    return lines[-1][:200] if lines else ""


def invoice_done(invoice_id: Optional[str], total: Optional[str],
                  currency: Optional[str], downloads_dir: str,
                  steps_log: Optional[list[dict]] = None) -> str:
    """The final "all done" reply after the operator approved."""
    out = ["Done."]
    if invoice_id:
        out.append("")
        out.append(
            f"Invoice **{invoice_id}** is in QuickBooks "
            f"({total} {currency or ''}). I did NOT send it — it's a "
            f"draft for your review."
        )
    out.append("")
    out.append(f"Receipt PDFs are under:\n  `{downloads_dir}`")
    if steps_log:
        out.append("")
        out.append("---")
        out.append("_Steps run:_")
        for s in steps_log:
            emoji = "✓" if s.get("ok") else "✗"
            out.append(f"_  {emoji} {s['name']} (exit {s.get('exit_code', '?')})_")
    return "\n".join(out)


def general_assistant_reply(text: str) -> str:
    """Wrap an assistant-generated reply. The assistant's text is
    already conversational; we strip markdown markers so Gmail
    web + Superhuman don't render the literal `**` / `##` / `---`."""
    return strip_markdown_for_plaintext_email((text or "").strip())


def workflow_suggestion_reply(text: str) -> str:
    """Case (b) — no approved workflow, no existing tools. The model
    drafts the three-section template (headline / 100-word /
    Claude Code prompt); we strip markdown markers before send."""
    return strip_markdown_for_plaintext_email((text or "").strip())


def ad_hoc_proposal_reply(text: str) -> str:
    """Case (c) — no approved workflow but SAI has tools. The model
    drafts a TLDR + STEPS + Approve y/n proposal; markdown stripped
    before send. The same wrapper is used for the post-execution
    status reply once the operator approves."""
    return strip_markdown_for_plaintext_email((text or "").strip())


def eval_acknowledged() -> str:
    """For EVAL_FEEDBACK route — brief confirmation."""
    return textwrap.dedent("""\
        Got it — I logged your label correction. The eval workflow
        will pick this up on its next pass. Nothing else needed
        from you on this thread.""").strip()


def clarification_needed(question: str) -> str:
    """When the bot needs more info from the operator."""
    return (
        f"{question.strip()}\n\n"
        "Reply on this thread when you're ready."
    )
