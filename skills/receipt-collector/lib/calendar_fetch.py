"""
calendar_fetch — read-only Google Calendar wrapper for the cost-compiler.

Reuses the SAI-wide Google OAuth at ~/.SAI/credentials.json + a per-skill
token at ~/.SAI/calendar_token.json (mode 0600). First run prompts an
OAuth flow that adds the Calendar read scope; subsequent runs use the
saved refresh token.

Per SAI #5 (least-privileged connectors), the scope is read-only.
Per #6 (fail closed), missing creds or scope fails before any fetch.

Public API:
    list_events(start, end, calendar_id="primary", time_zone="UTC")
        -> list[event_dict]  (Calendar API v3 event resource shape)

The dict shape matches what `lib/trip_calendar.py` already consumes:
{"id", "summary", "location", "description",
 "start": {"date" or "dateTime": ...},
 "end":   {"date" or "dateTime": ...}}
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DEFAULT_CREDS = "~/.SAI/credentials.json"
DEFAULT_TOKEN = "~/.SAI/calendar_token.json"


class CalendarScopeMissing(RuntimeError):
    """Raised when the saved token lacks Calendar read scope."""


def _build_service(
    creds_path: str = DEFAULT_CREDS,
    token_path: str = DEFAULT_TOKEN,
):
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ImportError(
            "google-api-python-client + google-auth-oauthlib required. "
            "Install:  python3 -m pip install --user "
            "google-api-python-client google-auth-oauthlib\n"
            f"({e})"
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

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_rfc3339(d: date | datetime, end_of_day: bool = False) -> str:
    """Produce an RFC3339 timestamp the Calendar API accepts as timeMin/timeMax."""
    if isinstance(d, datetime):
        # Ensure timezone-aware
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    # date → midnight or end-of-day
    if end_of_day:
        dt = datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)
    else:
        dt = datetime.combine(d, time(0, 0, 0), tzinfo=timezone.utc)
    return dt.isoformat()


def list_events(
    start: date,
    end: date,
    *,
    calendar_id: str = "primary",
    time_zone: str = "UTC",
    max_results: int = 250,
    creds_path: str = DEFAULT_CREDS,
    token_path: str = DEFAULT_TOKEN,
) -> list[dict]:
    """Return events between start (inclusive) and end (inclusive).

    Handles paging automatically. Returns the API's event resource
    dicts; downstream code only relies on `id`, `summary`, `location`,
    `description`, `start`, `end` (which `trip_calendar.py` already
    consumes).
    """
    svc = _build_service(creds_path=creds_path, token_path=token_path)
    time_min = _to_rfc3339(start, end_of_day=False)
    time_max = _to_rfc3339(end, end_of_day=True)
    events: list[dict] = []
    page_token: str | None = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
                timeZone=time_zone,
                pageToken=page_token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            events.append(ev)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events
