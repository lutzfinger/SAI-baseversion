"""Granola Personal API connector — generic SAI capability.

Lives in SAI-baseversion (public capability layer) per PRINCIPLES.md
public/private split: the HOW (talking to Granola) is public; the
WHAT-TO-FETCH (folder names, schedules) lives in the operator overlay.

Auth precondition
-----------------
Each connector instance is constructed with an `api_key` string. Callers
should resolve the key via `resolve_granola_api_key()` from this module
(or pass one explicitly for tests). The resolver tries these sources
in order, per PRINCIPLES.md "no passwords outside 1Password / Keychain":

  1. `api_key=...` explicit (caller-supplied; tests)
  2. Env var `GRANOLA_API_KEY` (typically populated from `keychain://…`
     by `scripts/runtime_secret_helpers.sh` at process start)
  3. 1Password CLI: `op read $GRANOLA_OP_REF` (env var, e.g.
     `op://Private/Granola API/credential`)
  4. macOS Keychain: `security find-generic-password -s Granola -a <user>`

The connector NEVER stores the key on disk and NEVER logs it.

Audit logging
-------------
Every API call appends a JSONL line to `~/Library/Logs/SAI/granola-client.jsonl`:
  {ts, endpoint, status_code, response_bytes, elapsed_ms, error?}
Key is never logged. Audit is append-only (§4).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib import error, parse, request

from app.connectors.base import ConnectorAction, ConnectorDescriptor

LOGGER = logging.getLogger(__name__)

# Default audit log location. Skills can override via init param.
_DEFAULT_AUDIT_LOG = Path.home() / "Library" / "Logs" / "SAI" / "granola-client.jsonl"


class GranolaConnectorError(RuntimeError):
    """Raised when the Granola API cannot be used safely."""


# ─── Key resolver (1Password / Keychain / env) ────────────────────────


def resolve_granola_api_key(
    *,
    explicit: Optional[str] = None,
) -> str:
    """Resolve the Granola API key without triggering interactive unlock.

    Per PRINCIPLES.md §7a — 1Password access MUST be service-account only.
    This function NEVER invokes `op read` directly. The canonical path:

      1. ``GRANOLA_API_KEY`` is already in the env — return it.
         Typically set by `scripts/with_1password.sh` (the canonical
         service-account wrapper) reading `~/.config/sai/runtime.env`,
         OR by `app.shared.runtime_env.load_runtime_env_best_effort()`
         which we call lazily on first access.
      2. ``GRANOLA_API_KEY_KEYCHAIN_REF`` points at a Keychain entry —
         resolve it via `security find-generic-password` (no 1P unlock).
      3. Otherwise: fail closed with a clear error pointing the operator
         at the runtime.env file.

    Args:
      explicit: if provided non-empty, returned directly (used in tests).

    Returns the key as a stripped string. Raises GranolaConnectorError
    if no source produced a non-empty key WITHOUT requiring interactive
    1Password unlock.
    """
    if explicit and explicit.strip():
        return explicit.strip()

    # First pass: maybe env is already populated. SAI's canonical name
    # is SAI_GRANOLA_API_KEY (per app/shared/config.py AliasChoices);
    # GRANOLA_API_KEY is a back-compat alias for the standalone scripts.
    for var in ("SAI_GRANOLA_API_KEY", "GRANOLA_API_KEY"):
        env_key = os.environ.get(var, "").strip()
        if env_key and not env_key.startswith("keychain://") and not env_key.startswith("op://"):
            return env_key

    # Second pass: load SAI runtime.env. The canonical loader resolves
    # `keychain://` and `op://` references safely (#7a compliant — `op`
    # only fires when OP_SERVICE_ACCOUNT_TOKEN is already in the env).
    try:
        from app.shared.runtime_env import load_runtime_env_best_effort
        load_runtime_env_best_effort()
    except Exception as exc:
        LOGGER.warning("could not load runtime.env: %s", exc)

    for var in ("SAI_GRANOLA_API_KEY", "GRANOLA_API_KEY"):
        env_key = os.environ.get(var, "").strip()
        if env_key and not env_key.startswith("keychain://") and not env_key.startswith("op://"):
            return env_key

    raise GranolaConnectorError(
        "Could not resolve Granola API key. Per PRINCIPLES.md §7a, route "
        "through `scripts/with_1password.sh` or pre-source "
        "`~/.config/sai/runtime.env` before invoking the connector. "
        "Expected env var: SAI_GRANOLA_API_KEY (canonical) or "
        "GRANOLA_API_KEY (back-compat). NEVER call `op read` directly "
        "from operator-facing scripts."
    )


# ─── Audit log ────────────────────────────────────────────────────────


def _append_audit(
    audit_log_path: Path,
    record: dict[str, Any],
) -> None:
    """Append one JSONL audit record. Best-effort — failures don't break callers."""
    try:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        record_out = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        with audit_log_path.open("a") as f:
            f.write(json.dumps(record_out) + "\n")
    except OSError as exc:
        LOGGER.warning("granola audit log write failed: %s", exc)


# ─── Connector ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GranolaNote:
    """Normalized note shape returned by the higher-level methods."""
    id: str
    title: str
    start_time: str            # ISO 8601 (may be empty)
    folder_names: tuple[str, ...]
    raw: dict[str, Any]        # full underlying response, for callers that need more


class GranolaPersonalAPIConnector:
    """Read Granola notes via the official public personal API.

    Default base URL is `https://public-api.granola.ai/v1` and matches
    what the existing `granola-keynote-sync` and `granola_note_review`
    workers use. Tests pass a stub via `_fetcher`.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = "https://public-api.granola.ai/v1",
        timeout_seconds: int = 30,
        list_path: str = "/notes",
        note_path_template: str = "/notes/{note_id}",
        audit_log_path: Path = _DEFAULT_AUDIT_LOG,
        _fetcher: Optional[Any] = None,    # test hook
    ) -> None:
        # Resolve key lazily — let the caller pass None and resolve only
        # when the first request fires. Tests inject _fetcher to skip auth.
        self._api_key_value: Optional[str] = api_key.strip() if api_key else None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.list_path = list_path
        self.note_path_template = note_path_template
        self.audit_log_path = audit_log_path
        self._fetcher = _fetcher

    @property
    def api_key(self) -> str:
        """Lazy-resolved key. Calls resolve_granola_api_key() on first access."""
        if not self._api_key_value:
            self._api_key_value = resolve_granola_api_key()
        return self._api_key_value

    # Some legacy callers (granola_note_review) read `.api_key` as a plain
    # attribute before any request fires, expecting an empty string when
    # not configured rather than an exception. Provide a setter that just
    # writes through to the underlying field.
    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key_value = (value or "").strip()

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.granola.authenticate",
                reason="Reading Granola notes requires an explicit Granola personal API key.",
            ),
            ConnectorAction(
                action="connector.granola.read_notes",
                reason="The workflow reads Granola note metadata, summaries, and transcripts.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            component_name="connector.granola-personal-api",
            source_details={
                "source": "granola_personal_api",
                "base_url": self.base_url,
                "list_path": self.list_path,
                "note_path_template": self.note_path_template,
            },
        )

    # ─── Legacy methods (preserve API compat with granola_note_review) ──

    def list_notes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Single-page list (legacy). For pagination + filtering use list_all_notes()."""
        payload = self._get_json(self._url(self.list_path, {"limit": str(max(1, min(limit, 200)))}))
        return _extract_notes_array(payload)

    def get_note(self, *, note_id: str) -> dict[str, Any]:
        """Fetch one note by id, including its transcript (legacy method name)."""
        url = self._url(
            self.note_path_template.format(note_id=parse.quote(note_id, safe="")),
            {"include": "transcript"},
        )
        payload = self._get_json(url)
        if isinstance(payload, dict):
            if isinstance(payload.get("note"), dict):
                return payload["note"]
            return payload
        raise GranolaConnectorError(f"Granola note response for {note_id!r} was malformed.")

    def load_fixture(self, path: Path) -> list[dict[str, Any]]:
        """Load notes from a JSON fixture file (used in tests)."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _extract_notes_array(payload)

    # ─── New higher-level methods ──────────────────────────────────────

    def list_all_notes(
        self,
        *,
        page_size: int = 200,
        max_pages: int = 50,
        since_iso: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Paginate through `/notes` and return every note stub.

        `since_iso` (optional) passed to the API if it supports filtering;
        otherwise filtering happens client-side on `created_at`.
        """
        out: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params = {"limit": str(page_size)}
            if cursor:
                params["cursor"] = cursor
            if since_iso:
                params["since"] = since_iso
            payload = self._get_json(self._url(self.list_path, params))
            page_notes = _extract_notes_array(payload)
            if not page_notes:
                break
            out.extend(page_notes)
            next_cursor = (
                payload.get("next_cursor") if isinstance(payload, dict) else None
            )
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        return out

    def list_folders(self, *, max_notes_to_scan: int = 200) -> list[dict[str, Any]]:
        """Return [{name, note_count}, ...] sorted by count desc.

        The Granola Personal API doesn't expose a /folders endpoint and
        the /notes list response doesn't include `folder_membership`.
        We fetch full details for the most recent `max_notes_to_scan`
        notes (capped, to bound API cost) and aggregate folders client-side.
        Each full-note GET counts against the connector's audit log.
        """
        notes = self.list_all_notes(page_size=min(max_notes_to_scan, 200))
        notes = notes[:max_notes_to_scan]
        counts: dict[str, int] = {}
        for stub in notes:
            try:
                full = self.get_note(note_id=str(stub["id"]))
            except GranolaConnectorError:
                continue
            for f in (full.get("folder_membership") or []):
                name = (f.get("name") if isinstance(f, dict) else None) or ""
                if name:
                    counts[name] = counts.get(name, 0) + 1
        return [
            {"name": name, "note_count": count}
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    def list_notes_in_folder(
        self,
        folder_name: str,
        *,
        start_date: Optional[str] = None,   # YYYY-MM-DD inclusive
        end_date: Optional[str] = None,     # YYYY-MM-DD inclusive
        fuzzy: bool = True,
        max_notes_to_scan: int = 200,
    ) -> list[GranolaNote]:
        """Return notes in a folder, optionally filtered to a date range.

        Cost-optimized path:
          1. Page the cheap /notes list endpoint (no folder info).
          2. Filter by date range FIRST (no extra API calls — uses
             `created_at` from the list response).
          3. For each date-passing note, fetch full details via GET
             /notes/{id} to inspect `folder_membership`.
          4. Filter by folder.

        For ~10–30 sessions over a week this is ~10–30 GETs total
        instead of fetching every note's details.

        Args:
          folder_name: exact or fuzzy folder title match.
          start_date / end_date: ISO YYYY-MM-DD; both inclusive; either
            optional. Compared against the note's `created_at` (date part).
          fuzzy: if True (default), match folder name case-insensitively
            and accept any folder containing the requested substring.
          max_notes_to_scan: hard cap on how many list-page notes get
            considered (top of the recency-sorted list). Keeps API cost
            bounded for high-volume accounts.

        Returns notes sorted by start_time ascending.
        """
        target = (folder_name or "").strip().lower()
        stubs = self.list_all_notes(page_size=min(max_notes_to_scan, 200))[:max_notes_to_scan]

        # Date-filter the cheap metadata first
        date_passing_stubs: list[dict[str, Any]] = []
        for s in stubs:
            start_time = s.get("created_at") or s.get("start_time") or s.get("started_at") or ""
            date_part = start_time[:10] if start_time else ""
            if start_date and date_part and date_part < start_date:
                continue
            if end_date and date_part and date_part > end_date:
                continue
            date_passing_stubs.append(s)

        # Fetch full details only for the survivors, filter by folder
        matches: list[GranolaNote] = []
        for stub in date_passing_stubs:
            try:
                full = self.get_note(note_id=str(stub["id"]))
            except GranolaConnectorError:
                continue
            folders = [
                (f.get("name") if isinstance(f, dict) else "") or ""
                for f in (full.get("folder_membership") or [])
            ]
            if not _folder_matches(folders, target, fuzzy):
                continue
            start_time = (
                full.get("created_at") or stub.get("created_at")
                or full.get("start_time") or ""
            )
            matches.append(GranolaNote(
                id=str(full.get("id", stub.get("id", ""))),
                title=str(full.get("title", stub.get("title", ""))),
                start_time=str(start_time),
                folder_names=tuple(f for f in folders if f),
                raw=full,
            ))
        matches.sort(key=lambda x: x.start_time)
        return matches

    def get_transcript(self, note_id: str) -> Optional[str]:
        """Return the raw transcript text for a note (or None if missing)."""
        note = self.get_note(note_id=note_id)
        ts = note.get("transcript")
        if ts is None or ts == "":
            return None
        return ts if isinstance(ts, str) else json.dumps(ts)

    def get_note_metadata(self, note_id: str) -> dict[str, Any]:
        """Like get_note but strips the transcript field. Useful for cheap listings."""
        note = self.get_note(note_id=note_id)
        return {k: v for k, v in note.items() if k != "transcript"}

    # ─── HTTP plumbing ─────────────────────────────────────────────────

    def _get_json(self, url: str) -> Any:
        # Tests can inject a fetcher to avoid hitting the network.
        if self._fetcher is not None:
            return self._fetcher(url)

        if not self.api_key:
            raise GranolaConnectorError("GRANOLA_API_KEY is not configured.")

        t0 = time.monotonic()
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "SAI-Granola-Client/2.0",
            },
            method="GET",
        )
        endpoint_for_audit = url.replace(self.api_key, "***REDACTED***")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read()
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                _append_audit(self.audit_log_path, {
                    "endpoint": endpoint_for_audit,
                    "status_code": response.status,
                    "response_bytes": len(body),
                    "elapsed_ms": elapsed_ms,
                })
                return json.loads(body.decode("utf-8"))
        except error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _append_audit(self.audit_log_path, {
                "endpoint": endpoint_for_audit,
                "status_code": exc.code,
                "elapsed_ms": elapsed_ms,
                "error": f"HTTPError {exc.code}: {err_body[:240]}",
            })
            raise GranolaConnectorError(
                f"Granola API request failed {exc.code}: {err_body[:240]}"
            ) from exc
        except error.URLError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _append_audit(self.audit_log_path, {
                "endpoint": endpoint_for_audit,
                "status_code": 0,
                "elapsed_ms": elapsed_ms,
                "error": f"URLError: {exc.reason}",
            })
            raise GranolaConnectorError(
                f"Could not reach Granola API at {self.base_url}: {exc.reason}"
            ) from exc

    def _url(self, path: str, query: dict[str, str] | None) -> str:
        url = f"{self.base_url}{path}"
        if not query:
            return url
        return f"{url}?{parse.urlencode(query)}"


# ─── Module-level helpers ─────────────────────────────────────────────


def _extract_notes_array(payload: Any) -> list[dict[str, Any]]:
    """Pull the notes list out of any of the response shapes Granola has used."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise GranolaConnectorError("Granola response was not a JSON list or dict.")
    for key in ("notes", "data", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            return [item for item in v if isinstance(item, dict)]
    # Allow an empty-body 'no results' shape
    return []


def _folder_matches(
    note_folders: Iterable[str],
    target_lower: str,
    fuzzy: bool,
) -> bool:
    if not target_lower:
        return False
    for f in note_folders:
        f_lower = (f or "").lower().strip()
        if not f_lower:
            continue
        if fuzzy:
            if target_lower in f_lower or f_lower in target_lower:
                return True
        else:
            if f_lower == target_lower:
                return True
    return False
