"""
Gmail thread + attachment downloader.

For each thread matching a Gmail query, save a folder under
<out_dir>/<thread_id>/ containing:

  * body.txt      — best-available plain text (text/plain part if present,
                    otherwise text/html stripped of tags)
  * body.html     — raw HTML body (only present if the message had a text/html part)
  * subject.txt   — the thread's subject line (handy for grep + filename matching)
  * <filename>    — every attachment, original filename preserved

OAuth: re-uses the SAI-wide Google OAuth at ~/.SAI/credentials.json. The
saved token at ~/.SAI/token.json must include the
`https://www.googleapis.com/auth/gmail.readonly` scope. If it doesn't, the
first run of this module prompts the user to re-authorise.

Note: this module imports lazily so the rest of the runner works even when
google-api-python-client is not installed.
"""
from __future__ import annotations

import base64
import html
import os
import re
from datetime import date
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_CREDS = "~/.SAI/credentials.json"
DEFAULT_TOKEN = "~/.SAI/gmail_token.json"


class GmailScopeMissing(RuntimeError):
    """Raised when the saved Google token lacks Gmail read scope."""


def _build_service(creds_path: str = DEFAULT_CREDS, token_path: str = DEFAULT_TOKEN):
    """Return an authenticated Gmail v1 service.

    Raises ImportError if google-api-python-client is missing.
    Raises GmailScopeMissing if the saved token has no gmail scope and we
    can't run an interactive flow (e.g., headless).
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ImportError(
            "google-api-python-client + google-auth-oauthlib are required.\n"
            "Install:  python3 -m pip install --user "
            "google-api-python-client google-auth-oauthlib\n"
            f"(Original error: {e})"
        )

    creds_path = os.path.expanduser(creds_path)
    token_path = os.path.expanduser(token_path)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"No Google OAuth client at {creds_path}. Drop the "
                    "Google Cloud OAuth client JSON there first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
            Path(token_path).write_text(creds.to_json())
            os.chmod(token_path, 0o600)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _walk_parts(payload: dict):
    """Yield every leaf MIME part (no children) under a Gmail payload."""
    if not payload:
        return
    parts = payload.get("parts")
    if not parts:
        yield payload
        return
    for p in parts:
        yield from _walk_parts(p)


def _safe_name(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.\- ]+", "_", s)
    return s[:max_len].strip().rstrip(".")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n")
_BLANK_RE = re.compile(r"\n{3,}")


def html_to_text(s: str) -> str:
    """Very small HTML → text shim. Good enough to make Uber/United receipts
    grep-able and readable without pulling in BeautifulSoup."""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = re.sub(r"(?i)</tr>", "\n", s)
    s = re.sub(r"(?i)</td>", "  ", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub("\n", s)
    s = _BLANK_RE.sub("\n\n", s)
    return s.strip()


def search_threads(service, query: str, max_threads: int = 200) -> list[str]:
    """Return matching thread IDs (paginated through, capped at max_threads)."""
    out: list[str] = []
    page_token = None
    while True:
        req = service.users().threads().list(
            userId="me", q=query, maxResults=min(100, max_threads - len(out)), pageToken=page_token,
        )
        resp = req.execute()
        out.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token or len(out) >= max_threads:
            break
    return out


def fetch_thread(service, thread_id: str) -> dict:
    """Fetch a Gmail thread and return its content in-memory — no disk writes.

    Returns:
      {
        "thread_id": str,
        "subject":   str,
        "date_iso":  str (best-effort first message internalDate as YYYY-MM-DD, or ""),
        "body_text": str (text/plain if available, otherwise HTML→text),
        "attachments": [(filename:str, content_type:str, bytes)],
      }
    """
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()

    subject = ""
    date_iso = ""
    text_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[tuple[str, str, bytes]] = []

    for msg_i, msg in enumerate(thread.get("messages", [])):
        if not subject:
            for h in (msg.get("payload") or {}).get("headers", []):
                if h.get("name", "").lower() == "subject":
                    subject = h.get("value", "")
                    break
        if msg_i == 0 and not date_iso:
            ms = int(msg.get("internalDate", "0") or 0)
            if ms:
                import datetime as _dt
                date_iso = _dt.date.fromtimestamp(ms // 1000).isoformat()

        for part in _walk_parts(msg.get("payload") or {}):
            mime = part.get("mimeType", "")
            body = part.get("body") or {}
            filename = part.get("filename") or ""
            data = body.get("data")
            att_id = body.get("attachmentId")

            if filename and att_id:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=msg["id"], id=att_id,
                ).execute()
                raw = base64.urlsafe_b64decode(att.get("data", "").encode())
                attachments.append((_safe_name(filename), mime or "application/octet-stream", raw))
            elif mime == "text/plain" and data:
                text_chunks.append(base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace"))
            elif mime == "text/html" and data:
                html_chunks.append(base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace"))

    if text_chunks:
        body_text = "\n\n---\n\n".join(text_chunks)
    elif html_chunks:
        body_text = "\n\n---\n\n".join(html_to_text(c) for c in html_chunks)
    else:
        body_text = ""

    body_html = "\n\n<!-- next message -->\n\n".join(html_chunks) if html_chunks else ""

    return {
        "thread_id": thread_id,
        "subject": subject,
        "date_iso": date_iso,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
    }


def download_thread(service, thread_id: str, out_dir: Path) -> dict:
    """Legacy disk-writing flow kept for download-receipts subcommand.

    New code should call fetch_thread() instead and route body_text
    directly into pdf_render.render_pdf().
    """
    info = fetch_thread(service, thread_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    files_written: list[str] = []
    if info["subject"]:
        (out_dir / "subject.txt").write_text(info["subject"] + "\n")
    if info["body_text"]:
        (out_dir / "body.txt").write_text(info["body_text"])
    for fname, _mime, raw in info["attachments"]:
        dest = out_dir / fname
        i = 1
        while dest.exists():
            stem, dot, ext = fname.rpartition(".")
            dest = out_dir / (f"{stem}_{i}.{ext}" if dot else f"{fname}_{i}")
            i += 1
        dest.write_bytes(raw)
        files_written.append(dest.name)
    return {
        "thread_id": thread_id,
        "subject": info["subject"],
        "out_dir": str(out_dir),
        "files": files_written,
        "has_text": bool(info["body_text"]),
        "has_html": False,  # legacy field; HTML no longer written
    }


def build_query(senders: list[str], start: date, end: date, keywords: list[str] | None = None) -> str:
    """Build a Gmail query string. Mirrors lib.gmail_search.build_receipt_query
    but adds the keywords/has:attachment hint for receipt threads."""
    parts = []
    if senders:
        sender_or = " OR ".join(f"from:{s}" for s in senders)
        parts.append(f"({sender_or})")
    parts.append(f"after:{start.strftime('%Y/%m/%d')}")
    parts.append(f"before:{(end).strftime('%Y/%m/%d')}")
    if keywords:
        parts.append("(" + " OR ".join(keywords) + ")")
    return " ".join(parts)
