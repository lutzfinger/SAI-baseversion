"""
Google Photos Library API helpers.

The operator's phone receipts often live in Google Photos under their
personal Google account — separate from the work-email account that
holds Gmail forwards. This module supports a *secondary* OAuth flow
that authenticates the personal account and saves its token under a
distinct filename (e.g. `gphotos_token_lutzT.json`) so it doesn't
collide with the main Gmail token.

Caveat: as of March 2025 Google restricted the Photos Library API for
third-party apps not on the partner program. Read access to user-owned
media may return empty results or 403. The lib still ships the search
machinery so that:
  * if Google reverses the restriction, the runner just works;
  * a SAI install that *is* on the partner program can use this directly.
If the API blocks, the runner falls back to the email-forward path.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

PHOTOS_SCOPE = "https://www.googleapis.com/auth/photoslibrary.readonly"
DEFAULT_CREDS = "~/.SAI/credentials.json"


def auth_for_account(account_label: str,
                     creds_path: str = DEFAULT_CREDS,
                     token_dir: str = "~/.SAI/") -> "Credentials":
    """Run an OAuth flow tied to a specific Google account label
    (e.g., 'lutzT', 'work'). Token is saved as gphotos_token_<label>.json
    so multiple accounts coexist.
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds_path = os.path.expanduser(creds_path)
    token_path = Path(os.path.expanduser(token_dir)) / f"gphotos_token_{account_label}.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), [PHOTOS_SCOPE])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except Exception: creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, [PHOTOS_SCOPE])
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
            os.chmod(token_path, 0o600)
    return creds


def search_media(creds, start: date, end: date, max_items: int = 200) -> list[dict]:
    """Return mediaItems created between start and end (inclusive).

    Returns list of dicts: {id, filename, mimeType, mediaMetadata, productUrl}.
    Raises if the API returns 403 (the March 2025 restriction).
    """
    import requests
    out: list[dict] = []
    page_token = None
    hdr = {"Authorization": f"Bearer {creds.token}",
           "Content-Type": "application/json"}
    body = {
        "filters": {
            "dateFilter": {
                "ranges": [{
                    "startDate": {"year": start.year, "month": start.month, "day": start.day},
                    "endDate":   {"year": end.year,   "month": end.month,   "day": end.day},
                }]
            },
            "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
        },
        "pageSize": 100,
    }
    while len(out) < max_items:
        if page_token:
            body["pageToken"] = page_token
        r = requests.post(
            "https://photoslibrary.googleapis.com/v1/mediaItems:search",
            headers=hdr, json=body, timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"photos search rc={r.status_code}: {r.text[:400]}")
        data = r.json()
        out.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out[:max_items]


def download_media(creds, media_item: dict, out_dir: Path) -> Path:
    """Download a single mediaItem to out_dir; returns the saved path."""
    import requests
    url = media_item.get("baseUrl")
    if not url:
        raise ValueError(f"mediaItem missing baseUrl: {media_item.get('id')}")
    # =d appends "download original" suffix per Google Photos API conventions
    r = requests.get(url + "=d", timeout=60)
    r.raise_for_status()
    name = media_item.get("filename") or f"{media_item.get('id', 'gphoto')}.jpg"
    dest = out_dir / name
    dest.write_bytes(r.content)
    return dest
