"""Helpers for a controlled LinkedIn data archive refresh workflow."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from app.connectors.linkedin_dataset import (
    linkedin_record_identity,
    load_linkedin_records,
    split_linkedin_name,
)
from app.workers.email_models import EmailMessage


class LinkedInArchiveConfig(BaseModel):
    """Static, non-secret controls for the LinkedIn archive flow."""

    request_url: str = "https://www.linkedin.com/mypreferences/d/download-my-data"
    allowed_host_suffixes: list[str] = Field(default_factory=lambda: ["linkedin.com"])
    ready_email_sender_domain_suffixes: list[str] = Field(default_factory=lambda: ["linkedin.com"])
    ready_subject_keywords: list[str] = Field(
        default_factory=lambda: [
            "download your data",
            "data archive",
            "archive is ready",
            "download is ready",
            "your archive is available",
        ]
    )
    ready_body_keywords: list[str] = Field(
        default_factory=lambda: [
            "download your data",
            "data archive",
            "archive is ready",
            "download is ready",
            "your archive is available",
        ]
    )
    archive_filename_patterns: list[str] = Field(
        default_factory=lambda: [
            "*linkedin*.zip",
            "*download*.zip",
            "*data*.zip",
            "*archive*.zip",
        ]
    )
    connections_filename_patterns: list[str] = Field(
        default_factory=lambda: [
            "*Connections*.csv",
            "*connections*.csv",
            "*Connections*.tsv",
            "*connections*.tsv",
            "*Connections*.xlsx",
            "*connections*.xlsx",
            "*Connections*.xls",
            "*connections*.xls",
            "*Contacts*.csv",
            "*contacts*.csv",
        ]
    )
    connections_required_columns: list[str] = Field(
        default_factory=lambda: ["First Name", "Last Name", "URL"]
    )
    fetch_wait_hours: int = 24
    ready_email_max_age_days: int = 7
    browser_action_wait_seconds: float = 4.0
    browser_navigation_wait_seconds: float = 6.0
    manual_login_timeout_seconds: int = 300
    manual_login_poll_seconds: float = 2.0

    @classmethod
    def load(cls, path: Path) -> LinkedInArchiveConfig:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"LinkedIn archive config must be a YAML mapping: {path}")
        return cls.model_validate(payload)


class LinkedInArchiveState(BaseModel):
    """Persistent local state for the archive request/download loop."""

    version: str = "1"
    last_request_at: datetime | None = None
    pending_request: bool = False
    ready_email_detected_at: datetime | None = None
    last_ready_email_message_id: str | None = None
    last_download_at: datetime | None = None
    last_archive_zip_path: str | None = None
    last_extract_dir: str | None = None
    last_connections_file_path: str | None = None
    last_status: str = "idle"
    last_error: str | None = None

    @classmethod
    def load(cls, path: Path) -> LinkedInArchiveState:
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )


class LinkedInArchiveEmailSignal(BaseModel):
    """Whether Gmail currently indicates that a fresh archive is available."""

    matched: bool
    message_id: str | None = None
    subject: str | None = None
    from_email: str | None = None
    received_at: datetime | None = None
    confidence: float = 0.0
    reason: str = "no_matching_email"


class LinkedInArchiveFetchDecision(BaseModel):
    """Whether the fetch step should attempt a download right now."""

    should_fetch: bool
    reason: str
    next_eligible_at: datetime | None = None


class LinkedInArchiveProcessResult(BaseModel):
    """Result of unpacking an archive and refreshing the canonical dataset."""

    status: str
    stored_zip_path: str
    extracted_dir: str
    selected_connections_file: str | None = None
    canonical_dataset_path: str | None = None
    canonical_dataset_updated: bool = False
    selected_file_format: str | None = None
    manual_review_required: bool = False
    extracted_file_count: int = 0
    previous_connection_count: int = 0
    imported_connection_count: int = 0
    new_connection_count: int = 0
    notes: list[str] = Field(default_factory=list)


def detect_ready_email(
    *,
    messages: list[EmailMessage],
    config: LinkedInArchiveConfig,
    now: datetime,
) -> LinkedInArchiveEmailSignal:
    """Return the strongest LinkedIn archive-ready email signal from Gmail."""

    max_age = timedelta(days=config.ready_email_max_age_days)
    best_match: LinkedInArchiveEmailSignal | None = None
    for message in sorted(
        messages,
        key=lambda item: item.received_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    ):
        sender = message.from_email.strip().lower()
        sender_domain = sender.rsplit("@", 1)[1] if "@" in sender else ""
        if not any(
            sender_domain.endswith(suffix.lower())
            for suffix in config.ready_email_sender_domain_suffixes
        ):
            continue
        received_at = message.received_at
        if received_at is not None and now - received_at > max_age:
            continue
        subject = message.subject.strip()
        body = message.body_excerpt.strip()
        subject_lower = subject.lower()
        body_lower = body.lower()
        subject_hit = any(keyword in subject_lower for keyword in config.ready_subject_keywords)
        body_hit = any(keyword in body_lower for keyword in config.ready_body_keywords)
        if not subject_hit and not body_hit:
            continue
        confidence = 0.9 if subject_hit else 0.7
        signal = LinkedInArchiveEmailSignal(
            matched=True,
            message_id=message.message_id,
            subject=subject or None,
            from_email=sender or None,
            received_at=received_at,
            confidence=confidence,
            reason="subject_keyword_match" if subject_hit else "body_keyword_match",
        )
        if best_match is None or signal.confidence > best_match.confidence:
            best_match = signal
    return best_match or LinkedInArchiveEmailSignal(matched=False)


def decide_fetch_attempt(
    *,
    state: LinkedInArchiveState,
    ready_email: LinkedInArchiveEmailSignal,
    config: LinkedInArchiveConfig,
    now: datetime,
) -> LinkedInArchiveFetchDecision:
    """Decide whether the download step should run now."""

    if not state.pending_request or state.last_request_at is None:
        return LinkedInArchiveFetchDecision(
            should_fetch=False,
            reason="no_pending_request",
        )
    if ready_email.matched:
        return LinkedInArchiveFetchDecision(
            should_fetch=True,
            reason="ready_email_detected",
        )
    next_eligible_at = state.last_request_at + timedelta(hours=config.fetch_wait_hours)
    if now >= next_eligible_at:
        return LinkedInArchiveFetchDecision(
            should_fetch=True,
            reason="wait_window_elapsed",
        )
    return LinkedInArchiveFetchDecision(
        should_fetch=False,
        reason="waiting_for_ready_email_or_timer",
        next_eligible_at=next_eligible_at,
    )


def find_latest_archive_zip(
    *,
    download_dir: Path,
    config: LinkedInArchiveConfig,
    requested_after: datetime | None = None,
) -> Path | None:
    """Find the freshest plausible LinkedIn zip in the watched download folder."""

    if not download_dir.exists():
        return None
    candidates: list[Path] = []
    for path in download_dir.glob("*.zip"):
        if not any(
            _matches_pattern_case_insensitive(path.name, pattern)
            for pattern in config.archive_filename_patterns
        ):
            continue
        if requested_after is not None:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified_at < requested_after:
                continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def process_downloaded_archive(
    *,
    zip_path: Path,
    archive_root: Path,
    canonical_dataset_path: Path,
    config: LinkedInArchiveConfig,
    now: datetime,
) -> LinkedInArchiveProcessResult:
    """Store, unpack, and refresh the canonical LinkedIn connections dataset."""

    stored_zips_dir = archive_root / "zips"
    extracted_dir = archive_root / "extracted" / now.strftime("%Y%m%dT%H%M%SZ")
    stored_zips_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    stored_zip_path = stored_zips_dir / zip_path.name
    shutil.copy2(zip_path, stored_zip_path)
    try:
        with ZipFile(stored_zip_path) as archive:
            archive.extractall(extracted_dir)
    except BadZipFile as error:
        return LinkedInArchiveProcessResult(
            status="invalid_zip",
            stored_zip_path=str(stored_zip_path),
            extracted_dir=str(extracted_dir),
            notes=[f"Archive could not be read as a zip file: {error}"],
        )

    extracted_files = [path for path in extracted_dir.rglob("*") if path.is_file()]
    selected_file = _select_connections_file(
        extracted_files=extracted_files,
        config=config,
    )
    if selected_file is None:
        return LinkedInArchiveProcessResult(
            status="connections_file_not_found",
            stored_zip_path=str(stored_zip_path),
            extracted_dir=str(extracted_dir),
            extracted_file_count=len(extracted_files),
            notes=["No plausible LinkedIn connections export was found in the archive."],
        )

    selected_suffix = selected_file.suffix.lower()
    previous_records = load_linkedin_records(canonical_dataset_path)
    imported_records = load_linkedin_records(selected_file)
    previous_identities = {
        identity
        for record in previous_records
        if (identity := linkedin_record_identity(record))
    }
    imported_identities = {
        identity
        for record in imported_records
        if (identity := linkedin_record_identity(record))
    }
    result = LinkedInArchiveProcessResult(
        status="processed",
        stored_zip_path=str(stored_zip_path),
        extracted_dir=str(extracted_dir),
        selected_connections_file=str(selected_file),
        selected_file_format=selected_suffix.lstrip(".") or None,
        extracted_file_count=len(extracted_files),
        previous_connection_count=len(previous_identities),
        imported_connection_count=len(imported_identities),
        new_connection_count=max(0, len(imported_identities - previous_identities)),
    )
    if selected_suffix == ".csv":
        canonical_dataset_path.parent.mkdir(parents=True, exist_ok=True)
        _rewrite_connections_export_as_csv(selected_file, canonical_dataset_path)
        result.canonical_dataset_path = str(canonical_dataset_path)
        result.canonical_dataset_updated = True
        result.notes.append("Canonical LinkedIn dataset refreshed from CSV export.")
        return result
    if selected_suffix == ".tsv":
        canonical_dataset_path.parent.mkdir(parents=True, exist_ok=True)
        _rewrite_connections_export_as_csv(selected_file, canonical_dataset_path)
        result.canonical_dataset_path = str(canonical_dataset_path)
        result.canonical_dataset_updated = True
        result.notes.append("Canonical LinkedIn dataset refreshed from TSV export.")
        return result

    result.status = "manual_review_required"
    result.manual_review_required = True
    result.notes.append(
        "A spreadsheet export was found, but only CSV/TSV files are auto-installed today."
    )
    return result


def _select_connections_file(
    *,
    extracted_files: list[Path],
    config: LinkedInArchiveConfig,
) -> Path | None:
    best_file: Path | None = None
    best_score = -1
    for path in extracted_files:
        score = _score_connections_candidate(path=path, config=config)
        if score > best_score:
            best_score = score
            best_file = path
    if best_score <= 0:
        return None
    return best_file


def _score_connections_candidate(*, path: Path, config: LinkedInArchiveConfig) -> int:
    name = path.name
    suffix = path.suffix.lower()
    score = 0
    if any(
        _matches_pattern_case_insensitive(name, pattern)
        for pattern in config.connections_filename_patterns
    ):
        score += 5
    lower_name = name.lower()
    if "connection" in lower_name:
        score += 3
    if lower_name == "connections.csv":
        score += 4
    if suffix == ".csv":
        score += 4
    elif suffix == ".tsv":
        score += 3
    elif suffix == ".xlsx":
        score += 2
    elif suffix == ".xls":
        score += 1
    else:
        return 0
    if suffix in {".csv", ".tsv"}:
        headers = _read_delimited_headers(path, delimiter="\t" if suffix == ".tsv" else ",")
        matched_headers = sum(
            1 for column in config.connections_required_columns if column in headers
        )
        score += matched_headers * 2
    return score


def _read_delimited_headers(path: Path, *, delimiter: str) -> set[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            headers = next(reader, [])
    except (OSError, StopIteration, UnicodeDecodeError, csv.Error):
        return set()
    return {header.strip() for header in headers if header.strip()}


def _rewrite_connections_export_as_csv(source_path: Path, destination_path: Path) -> None:
    rows = load_linkedin_records(source_path)
    fieldnames = [
        "First Name",
        "Last Name",
        "URL",
        "Email Address",
        "Company",
        "Position",
        "Connected On",
    ]
    with destination_path.open("w", encoding="utf-8", newline="") as destination_handle:
        writer = csv.DictWriter(destination_handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            first_name = (row.get("First Name") or "").strip()
            last_name = (row.get("Last Name") or "").strip()
            if not first_name and not last_name:
                first_name, last_name = split_linkedin_name(row.get("name", ""))
            writer.writerow(
                {
                    "First Name": first_name,
                    "Last Name": last_name,
                    "URL": (row.get("URL") or row.get("profile_url") or "").strip(),
                    "Email Address": (row.get("Email Address") or row.get("email") or "").strip(),
                    "Company": (row.get("Company") or "").strip(),
                    "Position": (row.get("Position") or "").strip(),
                    "Connected On": (row.get("Connected On") or "").strip(),
                }
            )


def _matches_pattern_case_insensitive(name: str, pattern: str) -> bool:
    return fnmatch(name.lower(), pattern.lower())
