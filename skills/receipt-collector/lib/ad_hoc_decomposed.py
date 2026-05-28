"""One-turn risk-tiered AD_HOC decomposition for the sai@ daemon.

Replaces the old two-turn propose→approve→execute AD_HOC flow for
tasks that decompose into read-only context-gathering + a single
reviewable write (e.g. "draft a reply to jane about my latest
Forbes article").

Flow (ONE operator turn):
  1. Decompose: one Claude call returns a JSON plan with
     `auto_steps` (read-only) + `write_steps` (the proposed draft) +
     a `final_response_template` with {{evidence:ID}} placeholders.
  2. Auto-execute the read-only steps NOW (Gmail search, Forbes
     latest) — they're free + no side effect, so no approval needed.
  3. Substitute the evidence into the template + render each
     write_step as a propose-for-approval block.
  4. Return the assembled reply. The WRITE step is NOT executed —
     the operator replies 'y' to run it (handled by the existing
     execute path), per PRINCIPLES.md #20 (suggest, never auto-apply)
     and #5 (policy before side effects).

Why deterministic substitution instead of a 2nd LLM call: the
read-only results are facts; the template is operator-approved
wording; one LLM call keeps cost down and removes a hallucination
surface.

Read-only tools available here:
  - gmail_search(query, max_results)   → list of message rows
  - forbes_latest(n)                   → list of recent Forbes articles

Both are READ ONLY. Write steps (gmail_create_draft) are described,
never executed, in this module.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# Operator-specific default; overlay can override via
# overlay["forbes_articles_root"]. Note operator spelling ("Fobes").
_DEFAULT_FORBES_ROOT = Path.home() / "Lutz_Media" / "Lutz-author" / "Fobes-Lutz-Author"


DECOMPOSE_SYSTEM_PROMPT = """\
You decompose ONE operator task (emailed to sai@) into risk-tiered steps.

The task is known to be doable today: SAI can search the operator's
Gmail (read-only), look up the operator's most-recent Forbes articles
(read-only), and stage a Gmail DRAFT for the operator to review (a
draft is reviewable — it is NOT sent until the operator approves).

Return ONE JSON object, no prose, no code fences:

{
  "auto_steps": [
    {"step_id": "step_1_...", "kind": "gmail_search", "args": {"query": "<gmail query>", "max_results": <1-20>}},
    {"step_id": "step_2_...", "kind": "forbes_latest", "args": {"n": <1-10>}}
  ],
  "write_steps": [
    {"step_id": "step_3_...", "kind": "gmail_create_draft", "proposed_action": "<one line, <=140 chars>"}
  ],
  "final_response_template": "<plain-text email body with {{evidence:STEP_ID}} and {{write_step:STEP_ID}} placeholders>"
}

Rules:
- `auto_steps[*].kind` MUST be one of: "gmail_search", "forbes_latest".
  These are the only read-only tools. If the task needs a read-only
  capability not in this list, return auto_steps: [] and explain in
  the template that the context can't be gathered.
- `write_steps[*].kind` MUST be "gmail_create_draft" (draft only — never
  a send). At most ONE write step.
- For a "draft a reply to <person> about <topic>" task: auto_steps =
  [gmail_search for the person, forbes_latest if the topic is an
  article], write_steps = [the draft].
- `final_response_template` is plain text. NO markdown (`**`, `##`,
  `[](url)`, ```). Use {{evidence:STEP_ID}} where each auto_step's
  results should appear, and {{write_step:STEP_ID}} where each
  write step's propose-for-approval block should appear. End with a
  line telling the operator to reply 'y' to run the write step.
- Every {{evidence:X}} placeholder MUST match an auto_steps step_id.
  Every {{write_step:X}} MUST match a write_steps step_id.

Canonical example — request: "draft a Gmail reply to jane about my
latest Forbes article, 3-sentence summary with the link":

{
  "auto_steps": [
    {"step_id": "step_1_find_jane", "kind": "gmail_search", "args": {"query": "from:jane OR jane", "max_results": 5}},
    {"step_id": "step_2_forbes", "kind": "forbes_latest", "args": {"n": 3}}
  ],
  "write_steps": [
    {"step_id": "step_3_draft", "kind": "gmail_create_draft", "proposed_action": "Draft a 3-sentence reply to the chosen Jane about the latest Forbes article, with the link."}
  ],
  "final_response_template": "I don't have a pre-approved skill for this, but I did the read-only parts already.\\n\\nAUTO-EXECUTED (read-only, no cost):\\n\\n{{evidence:step_1_find_jane}}\\n\\n{{evidence:step_2_forbes}}\\n\\nNEEDS YOUR APPROVAL:\\n\\n{{write_step:step_3_draft}}\\n\\nReply 'y' to create the draft, or 'use jane <email>' / 'use article <N>' to steer. Nothing is sent without your review."
}
"""


# --------------------------------------------------------------------------
# Read-only tool adapters
# --------------------------------------------------------------------------


def gmail_search(query: str, max_results: int = 5) -> list[dict]:
    """Read-only Gmail search via the daemon's existing OAuth.

    Returns a list of {from_email, from_name, subject, snippet,
    date_iso, days_ago}. Dedups by from_email so 5 messages from the
    same person collapse to one row (keeps the operator-facing list
    crisp)."""
    from lib import gmail_fetch  # daemon's authenticated Gmail builder

    service = gmail_fetch._build_service()
    listing = (
        service.users().messages()
        .list(userId="me", q=query, maxResults=max_results, includeSpamTrash=False)
        .execute()
    )
    ids = [m["id"] for m in (listing.get("messages") or [])]
    seen_emails: set[str] = set()
    rows: list[dict] = []
    for mid in ids:
        msg = (
            service.users().messages()
            .get(userId="me", id=mid, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = (msg.get("payload") or {}).get("headers") or []

        def _h(name: str) -> str:
            for h in headers:
                if (h.get("name") or "").lower() == name.lower():
                    return h.get("value") or ""
            return ""

        raw_from = _h("From")
        name, email = _split_from(raw_from)
        if email in seen_emails:
            continue
        seen_emails.add(email)
        date_iso, days_ago = _parse_date(_h("Date"))
        rows.append({
            "from_email": email,
            "from_name": name,
            "subject": _h("Subject"),
            "snippet": (msg.get("snippet") or "")[:160],
            "date_iso": date_iso,
            "days_ago": days_ago,
        })
    return rows


def forbes_latest(n: int = 3, root: Optional[str] = None) -> list[dict]:
    """Read-only scan of the operator's local Forbes markdown archive.

    Returns the top-N by frontmatter date (desc):
    [{title, date_iso, url, summary_snippet}]. File scan; no API cost.
    """
    base = Path(root) if root else _DEFAULT_FORBES_ROOT
    if not base.exists():
        raise FileNotFoundError(f"Forbes root not found: {base}")
    rows: list[dict] = []
    for path in base.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = _frontmatter(text)
        date_iso = _coerce_date(fm.get("date") or fm.get("published")) or \
            datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        title = fm.get("title") or path.stem.replace("-", " ").title()
        url = fm.get("url") or fm.get("link") or ""
        snippet = body.strip().split("\n\n", 1)[0].replace("\n", " ")[:160]
        rows.append({"title": title, "date_iso": date_iso, "url": url,
                     "summary_snippet": snippet})
    rows.sort(key=lambda r: r["date_iso"], reverse=True)
    return rows[:n]


_READ_ONLY_TOOLS: dict[str, Callable[..., list[dict]]] = {
    "gmail_search": lambda args: gmail_search(
        query=args.get("query", ""), max_results=int(args.get("max_results", 5))),
    "forbes_latest": lambda args: forbes_latest(n=int(args.get("n", 3))),
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _split_from(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        name, _, rest = raw.partition("<")
        return name.strip().strip('"'), rest.partition(">")[0].strip()
    return "", raw


def _parse_date(raw: str) -> tuple[str, int]:
    if not raw:
        return "", -1
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S", "%d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.date().isoformat(), max(0, (datetime.now(timezone.utc) - dt).days)
        except ValueError:
            continue
    return "", -1


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            v = v.strip().strip('"').strip("'")
            if v:
                fm[k.strip()] = v
    return fm, parts[2].lstrip("\n")


def _coerce_date(value: Any) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    for fmt in ("%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _format_evidence(step_id: str, kind: str, rows: list[dict], error: Optional[str]) -> str:
    head = f"{step_id} ({kind}):"
    if error:
        return f"{head}\n  ERROR: {error}"
    if not rows:
        return f"{head}\n  (no results)"
    lines = [head]
    for i, r in enumerate(rows, 1):
        kv = ", ".join(f"{k}={v}" for k, v in r.items() if v not in (None, "", -1))
        lines.append(f"  {i}. {kv}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# orchestrator
# --------------------------------------------------------------------------


def propose_decomposed(
    *,
    text: str,
    overlay: dict,
    claude_loop_fn: Callable,
    model: Optional[str] = None,
) -> Optional[str]:
    """One-turn decomposed AD_HOC. Returns the reply body, or None if the
    task didn't decompose (caller falls back to the old propose flow).

    `claude_loop_fn` is general_assistant._run_claude_loop, injected to
    avoid a circular import + to reuse the daemon's cost cap + audit.
    """
    inv = claude_loop_fn(
        system_prompt=DECOMPOSE_SYSTEM_PROMPT,
        user_text=text,
        overlay=overlay,
        mode="ad_hoc_decompose",
        use_web_search=False,
        model=model,
    )
    raw = (inv.final_text or "").strip()
    # Strip accidental code fences.
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        plan = json.loads(raw)
    except Exception:
        return None  # couldn't decompose → caller falls back

    auto_steps = plan.get("auto_steps") or []
    write_steps = plan.get("write_steps") or []
    template = plan.get("final_response_template") or ""
    if not auto_steps and not write_steps:
        return None  # nothing to do → fall back

    # Auto-execute the read-only steps. Per-step errors captured, not fatal.
    body = template
    forbes_root = (overlay or {}).get("forbes_articles_root")
    for step in auto_steps:
        sid = step.get("step_id", "")
        kind = step.get("kind", "")
        args = step.get("args") or {}
        if forbes_root and kind == "forbes_latest":
            args = {**args, "root": forbes_root}
        error = None
        rows: list[dict] = []
        if kind not in _READ_ONLY_TOOLS:
            error = f"unknown read-only tool: {kind}"
        else:
            try:
                if kind == "forbes_latest" and forbes_root:
                    rows = forbes_latest(n=int(args.get("n", 3)), root=forbes_root)
                else:
                    rows = _READ_ONLY_TOOLS[kind](args)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
        body = body.replace(f"{{{{evidence:{sid}}}}}", _format_evidence(sid, kind, rows, error))

    for step in write_steps:
        sid = step.get("step_id", "")
        kind = step.get("kind", "")
        action = step.get("proposed_action", "")
        block = f"{sid} ({kind}) — NEEDS APPROVAL\n  Proposed: {action}"
        body = body.replace(f"{{{{write_step:{sid}}}}}", block)

    return body


# ==========================================================================
# Turn 2 — operator approved (or steered): build + create the Gmail draft
# ==========================================================================

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def is_draft_intent(proposal_text: str) -> bool:
    """True if the turn-1 proposal proposed a gmail_create_draft write step."""
    return "gmail_create_draft" in (proposal_text or "")


def extract_email(text: str) -> Optional[str]:
    """Pull the first email address out of operator steering text.

    "no not to this jane but jane.alt@example.org" -> "jane.alt@example.org".
    Returns None when the reply has no address (then a clean 'no' is a
    real reject, not steering)."""
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


_DRAFT_BUILDER_SYSTEM_PROMPT = """\
You are creating ONE Gmail DRAFT reply on the operator's behalf. The
draft is NEVER sent automatically — it lands in the operator's Drafts
folder for them to review and send. So you may produce it directly.

You are given:
  - The operator's original request.
  - AUTO-EXECUTED evidence: candidate recipient(s) found in Gmail
    (with snippets — use these to detect the recipient's main
    language) + the operator's most-recent Forbes articles
    (title + url).
  - Optional operator steering feedback (e.g. "no, use bob@x.com" or
    "use article 2"). When present, the feedback OVERRIDES defaults.

Resolve:
  - recipient_email: the address to draft to. If steering names an
    address, use it. If EXACTLY ONE candidate's name matches the
    requested person, USE IT directly — do NOT ask. Only set
    needs_clarification=true when there are genuinely 2+ different
    people matching the name with no way to choose, OR zero matches.
    A single clear match (e.g. one "Jane Doe" for "jane") is NOT
    ambiguous — draft to them. Don't second-guess context from
    snippet topics.
  - article: the Forbes article to summarize. Default to the most
    recent (first in the evidence) unless steering says otherwise.
  - detected_language: the recipient's main language inferred from
    their email snippets ("German", "English", ...). Default English
    if unclear. WRITE THE DRAFT IN THAT LANGUAGE.
  - body: a warm, 3-sentence reply in detected_language. Opener +
    the article's gist + the URL on its own line. Plain text only —
    no markdown, no link syntax.

Return ONE JSON object, no prose, no code fences:
{
  "needs_clarification": false,
  "clarification_question": "",
  "recipient_email": "<addr>",
  "recipient_name": "<name or ''>",
  "detected_language": "<language>",
  "article_title": "<title>",
  "article_url": "<url>",
  "subject": "<draft subject>",
  "body": "<3-sentence reply in detected_language ending with the URL line>"
}

If you cannot resolve a recipient or article, set
needs_clarification=true + a one-line clarification_question.
"""


_TASK_ROUTER_PROMPT = """\
Classify ONE operator task (emailed to sai@) into a kind + extract the
hints needed to gather context. Return ONE JSON object, no prose:

{
  "task_kind": "draft_email" | "calendar_block" | "other",
  "recipient_hint": "<for draft_email: the person to write to, e.g. 'jane'>",
  "topic_hint": "<for draft_email: what to write about, e.g. 'latest forbes article'>",
  "calendar_day_hint": "<for calendar_block: 'tomorrow' | a date | ''>",
  "origin_hint": "<for calendar_block: starting place, e.g. 'Mountain View'>",
  "dest_hint": "<for calendar_block: destination/event, e.g. 'dinner'>"
}

Rules:
- "draft a reply / draft an email to <person> about <topic>" -> draft_email.
- "book/block travel time", "block time to <event>", "add a calendar
  block" -> calendar_block.
- Anything needing an IRREVERSIBLE action (send an email now, pay,
  post publicly) or a capability not listed -> "other".
- Creating a Gmail DRAFT and creating a CALENDAR EVENT are both
  REVERSIBLE/low-risk (operator reviews/deletes) — they are NOT
  "other".
"""


_CALENDAR_BUILDER_PROMPT = """\
You are creating ONE calendar event (a travel-time block) on the
operator's behalf. The event is reversible — the operator edits or
deletes it. So produce it directly.

Given:
  - The operator's original request.
  - The dinner/target event found on the calendar (summary, start,
    location) if any.
  - An estimated travel duration in minutes (rough).
  - Optional operator steering feedback (overrides defaults).

Produce a travel block that ENDS at the target event's start, lasting
the estimated duration. Return ONE JSON object, no prose:
{
  "needs_clarification": false,
  "clarification_question": "",
  "summary": "Travel: <origin> -> <dest>",
  "start_iso": "<RFC3339 start>",
  "end_iso": "<RFC3339 end = target event start>",
  "location": "<origin or route>",
  "description": "<one line: auto-created travel block, rough estimate>"
}
If the target event or its start time can't be resolved, set
needs_clarification=true + a one-line question.
"""


def create_gmail_draft(*, to: str, subject: str, body: str) -> str:
    """Create a Gmail DRAFT (never sends). Returns the draft id.

    Uses the daemon's send-capable service (GMAIL_SEND_SCOPES includes
    gmail.modify, which permits drafts().create()). Lazy-imports
    email_runner to avoid a circular import at module load.
    """
    import base64
    from email.message import EmailMessage

    from lib import email_runner  # send-capable _build_service (gmail.modify)

    svc = email_runner._build_service()
    msg = EmailMessage()
    msg["To"] = to
    if subject:
        msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = (
        svc.users().drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    return resp.get("id", "")


def build_and_create_draft(
    *,
    original_request: str,
    proposal: str,
    operator_feedback: Optional[str],
    overlay: dict,
    claude_loop_fn: Callable,
    model: Optional[str] = None,
) -> str:
    """Turn-2 handler. Resolve recipient+article+body via one LLM call,
    then CREATE the Gmail draft (reviewable; never sent). Returns the
    operator-facing status reply.

    When the LLM can't resolve a recipient/article (e.g. multiple
    Janes, no steering), returns the clarification question and creates
    NOTHING — fail-closed, intent stays open for the operator's next
    turn (#16g)."""
    user_text = (
        f"ORIGINAL REQUEST:\n{original_request.strip()}\n\n"
        f"TURN-1 PROPOSAL (contains the evidence):\n{proposal.strip()}\n\n"
        f"OPERATOR STEERING FEEDBACK (may be empty):\n"
        f"{(operator_feedback or '').strip()}\n"
    )
    inv = claude_loop_fn(
        system_prompt=_DRAFT_BUILDER_SYSTEM_PROMPT,
        user_text=user_text,
        overlay=overlay,
        mode="ad_hoc_draft_build",
        use_web_search=False,
        model=model,
    )
    raw = (inv.final_text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        plan = json.loads(raw)
    except Exception:
        return ("I couldn't assemble the draft (planner returned an "
                "unparseable result). Reply with the exact recipient "
                "and I'll try again.")

    if plan.get("needs_clarification"):
        q = plan.get("clarification_question") or \
            "Which recipient should I draft to?"
        return f"Before I create the draft: {q}"

    to = (plan.get("recipient_email") or "").strip()
    if not to or not _EMAIL_RE.match(to):
        return ("I don't have a valid recipient address yet. Reply with "
                "the email address to draft to (e.g. 'use bob@example.com').")

    subject = plan.get("subject") or "Re: your note"
    draft_body = plan.get("body") or ""
    try:
        draft_id = create_gmail_draft(to=to, subject=subject, body=draft_body)
    except Exception as exc:  # noqa: BLE001
        return (f"I built the draft but couldn't save it to Gmail "
                f"(error: {type(exc).__name__}: {exc}). Nothing was sent.")

    name = plan.get("recipient_name") or to
    article = plan.get("article_title") or "your latest article"
    return (
        f"Done — draft created in your Drafts folder.\n\n"
        f"To: {name} <{to}>\n"
        f"About: {article}\n"
        f"Subject: {subject}\n\n"
        f"--- draft body ---\n{draft_body}\n--- end ---\n\n"
        f"Nothing was sent. Open Drafts, review, and send when you're happy. "
        f"Reply here with edits and I'll redo it."
    )


# ==========================================================================
# Turn-1 AUTO-EXECUTE (2026-05-28): do the low-risk work immediately,
# reply terse, tag SAI/plan. Reversible writes (Gmail draft, calendar
# block) are created directly; only irreversible actions stay gated.
# ==========================================================================

# The meeting calendar token carries BOTH calendar.readonly + calendar.events,
# so one token serves read + write. Operator-specific path (#17 value).
_MEETING_CAL_TOKEN = (
    Path.home() / "Library" / "Application Support" / "SAI"
    / "tokens" / "meeting_calendar_token.json"
)
_CAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def _calendar_service(token_path: Optional[Path] = None):
    """Build a Calendar v1 service from the write-capable meeting token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    tp = token_path or _MEETING_CAL_TOKEN
    if not tp.exists():
        raise FileNotFoundError(
            f"No calendar token at {tp}. Calendar write needs the meeting "
            "calendar token (calendar.events scope)."
        )
    creds = Credentials.from_authorized_user_file(str(tp), _CAL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def find_calendar_events(day_hint: str = "tomorrow", max_results: int = 10) -> list[dict]:
    """Read-only: list events for the hinted day (default tomorrow).
    Returns [{summary, start_iso, end_iso, location}]."""
    from datetime import timedelta
    svc = _calendar_service()
    now = datetime.now(timezone.utc)
    # crude day resolution: tomorrow unless hint looks like a date
    base = now + timedelta(days=1)
    if day_hint and day_hint.lower() not in ("tomorrow", ""):
        try:
            base = datetime.fromisoformat(day_hint[:10]).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    resp = svc.events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=max_results,
    ).execute()
    rows = []
    for ev in resp.get("items", []):
        rows.append({
            "summary": ev.get("summary", ""),
            "start_iso": (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date", ""),
            "end_iso": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date", ""),
            "location": ev.get("location", ""),
        })
    return rows


def create_calendar_event(*, summary: str, start_iso: str, end_iso: str,
                          location: str = "", description: str = "") -> str:
    """Create a calendar event (reversible). Returns the event id."""
    svc = _calendar_service()
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return ev.get("id", "")


def estimate_route_minutes(*, origin: str, dest: str, claude_loop_fn: Callable,
                           overlay: dict) -> int:
    """LLM estimate of travel minutes (no maps API). Rough by design."""
    inv = claude_loop_fn(
        system_prompt=(
            "Estimate one-way driving minutes between two places. Reply with "
            "ONLY an integer number of minutes, nothing else. If unsure, give "
            "a sensible rough number."
        ),
        user_text=f"From: {origin}\nTo: {dest}\nMinutes:",
        overlay=overlay, mode="route_estimate", use_web_search=False,
    )
    m = re.search(r"\d{1,3}", inv.final_text or "")
    return int(m.group(0)) if m else 45


# ---- terse formatters (operator's exact shape) ------------------------

def format_terse_draft(*, recipient_email: str, language: str,
                       article_title: str, body: str) -> str:
    return (
        "Auto Execution, since low risk\n"
        f"- Found recipient: {recipient_email}\n"
        f"- Found latest Forbes Article: {article_title}\n"
        f"- Checked main language in current emails: {language}\n"
        f"- Drafted Email: \"{body}\"\n"
        "- Email is in Drafts. Ready for you to send."
    )


def format_terse_calendar(*, dinner_summary: str, origin: str,
                          minutes: int, start_iso: str, end_iso: str) -> str:
    return (
        "Auto Execution, since low risk\n"
        f"- Found target event: {dinner_summary}\n"
        f"- Origin: {origin}\n"
        f"- Estimated travel: ~{minutes} min (rough)\n"
        f"- Created calendar block: {start_iso} to {end_iso}\n"
        "- Block is on your calendar. Adjust or delete it there."
    )


# ---- the orchestrator --------------------------------------------------

def auto_execute_ad_hoc(*, text: str, overlay: dict, claude_loop_fn: Callable,
                        operator_feedback: Optional[str] = None,
                        model: Optional[str] = None) -> dict:
    """Turn-1 auto-execute. Returns:
      {"reply_text": str|None, "status_label": "SAI/plan"|"SAI/proposal",
       "did_write": bool, "kind": str}
    reply_text=None means "not auto-executable" → caller falls back.
    """
    def _llm_json(system, user):
        inv = claude_loop_fn(system_prompt=system, user_text=user,
                             overlay=overlay, mode="ad_hoc_autoexec",
                             use_web_search=False, model=model)
        raw = re.sub(r"^```(?:json)?|```$", "", (inv.final_text or "").strip(),
                     flags=re.MULTILINE).strip()
        try:
            return json.loads(raw)
        except Exception:
            return None

    route = _llm_json(_TASK_ROUTER_PROMPT, text)
    if not route:
        return {"reply_text": None, "status_label": "SAI/proposal",
                "did_write": False, "kind": "other"}
    kind = route.get("task_kind", "other")

    # ---- draft_email -------------------------------------------------
    if kind == "draft_email":
        query = (route.get("recipient_hint") or "").strip() or "jane"
        # Widen recall: search the name as sender OR anywhere, and exclude
        # the operator's own address so the meta-thread noise (all from
        # hello@) doesn't crowd out the actual person. Bigger max_results
        # since dedup-by-sender collapses duplicates.
        gmail_q = f"({query}) -from:hello@example.com -from:owner@example.com"
        try:
            cands = gmail_search(query=gmail_q, max_results=15)
        except Exception:
            cands = []
        if not cands:  # fallback: include everything (operator may BE the contact)
            try:
                cands = gmail_search(query=query, max_results=15)
            except Exception:
                cands = []
        try:
            arts = forbes_latest(n=3, root=(overlay or {}).get("forbes_articles_root"))
        except Exception:
            arts = []
        evidence = (
            "RECIPIENT CANDIDATES (with snippets for language):\n"
            + json.dumps(cands, indent=2) + "\n\nFORBES ARTICLES:\n"
            + json.dumps(arts, indent=2)
        )
        user = (f"ORIGINAL REQUEST:\n{text}\n\nEVIDENCE:\n{evidence}\n\n"
                f"OPERATOR FEEDBACK (may be empty):\n{operator_feedback or ''}")
        plan = _llm_json(_DRAFT_BUILDER_SYSTEM_PROMPT, user)
        if not plan or plan.get("needs_clarification"):
            q = (plan or {}).get("clarification_question", "Which recipient?")
            return {"reply_text": f"Before I draft: {q}",
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        to = (plan.get("recipient_email") or "").strip()
        if not _EMAIL_RE.match(to):
            return {"reply_text": "I need a valid recipient address (e.g. "
                    "'use bob@example.com').", "status_label": "SAI/proposal",
                    "did_write": False, "kind": kind}
        body = plan.get("body") or ""
        # Independent pre-write sanity gate (#21): a DIFFERENT model than the
        # Haiku draft-builder checks the recipient is grounded + the body
        # invents no Forbes claim, BEFORE the draft is created. FAIL →
        # block + downgrade to SAI/proposal (operator decision 2026-05-28);
        # a reversible draft to the wrong person is still one click from
        # being mis-sent, so we hold it rather than create-and-warn.
        from lib.pre_write_critique import critique_draft

        verdict = critique_draft(
            request_text=text,
            recipient_email=to,
            draft_body=body,
            candidate_emails=[c.get("from_email", "") for c in cands],
            forbes_evidence=arts,
            claude_loop_fn=claude_loop_fn,
            overlay=overlay,
        )
        if verdict.verdict == "failed":
            return {"reply_text": (
                        f"Held the draft before saving it — {verdict.reason} "
                        "Reply with the correct recipient/details and I'll redo it."),
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        try:
            create_gmail_draft(to=to, subject=plan.get("subject") or "Re:",
                               body=body)
        except Exception as e:
            return {"reply_text": f"Built the draft but couldn't save it "
                    f"({type(e).__name__}: {e}). Nothing sent.",
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        reply = format_terse_draft(
            recipient_email=to, language=plan.get("detected_language", "English"),
            article_title=plan.get("article_title", "your latest article"),
            body=body)
        return {"reply_text": reply, "status_label": "SAI/plan",
                "did_write": True, "kind": kind}

    # ---- calendar_block ----------------------------------------------
    if kind == "calendar_block":
        origin = (route.get("origin_hint") or "").strip() or "home"
        dest = (route.get("dest_hint") or "").strip() or "dinner"
        day = (route.get("calendar_day_hint") or "tomorrow").strip()
        try:
            events = find_calendar_events(day_hint=day)
        except Exception as e:
            return {"reply_text": f"Couldn't read your calendar "
                    f"({type(e).__name__}: {e}).",
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        minutes = estimate_route_minutes(origin=origin, dest=dest,
                                         claude_loop_fn=claude_loop_fn,
                                         overlay=overlay)
        user = (f"ORIGINAL REQUEST:\n{text}\n\nCANDIDATE EVENTS:\n"
                f"{json.dumps(events, indent=2)}\n\nESTIMATED MINUTES: {minutes}\n"
                f"ORIGIN: {origin}\nFEEDBACK: {operator_feedback or ''}")
        plan = _llm_json(_CALENDAR_BUILDER_PROMPT, user)
        if not plan or plan.get("needs_clarification"):
            q = (plan or {}).get("clarification_question",
                                 "Which event should I block travel time for?")
            return {"reply_text": f"Before I block time: {q}",
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        try:
            create_calendar_event(
                summary=plan.get("summary", f"Travel: {origin} -> {dest}"),
                start_iso=plan["start_iso"], end_iso=plan["end_iso"],
                location=plan.get("location", origin),
                description=plan.get("description", "Auto-created travel block (rough estimate)."))
        except Exception as e:
            return {"reply_text": f"Built the block but couldn't save it "
                    f"({type(e).__name__}: {e}).",
                    "status_label": "SAI/proposal", "did_write": False,
                    "kind": kind}
        reply = format_terse_calendar(
            dinner_summary=plan.get("summary", dest), origin=origin,
            minutes=minutes, start_iso=plan["start_iso"], end_iso=plan["end_iso"])
        return {"reply_text": reply, "status_label": "SAI/plan",
                "did_write": True, "kind": kind}

    # ---- other → not auto-executable ---------------------------------
    return {"reply_text": None, "status_label": "SAI/proposal",
            "did_write": False, "kind": "other"}
