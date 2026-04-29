"""Local LinkedIn dataset lookup for meeting enrichment."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor


class LinkedInDatasetConnector:
    """Lookup contact familiarity in a local user-supplied dataset."""

    def __init__(self, *, dataset_path: Path | None) -> None:
        self.dataset_path = dataset_path
        self._records: list[dict[str, str]] | None = None

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.linkedin.read_dataset",
                reason="Meeting decisions use a local LinkedIn dataset to assess familiarity.",
            )
        ]

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            component_name="connector.linkedin-dataset",
            source_details={
                "dataset_path": str(self.dataset_path) if self.dataset_path else "",
                "dataset_available": bool(self.dataset_path and self.dataset_path.exists()),
            },
        )

    def lookup_contact(
        self,
        *,
        email: str,
        display_name: str | None,
    ) -> dict[str, Any]:
        if self.dataset_path is None or not self.dataset_path.exists():
            return {
                "dataset_available": False,
                "matched": False,
                "matched_by": None,
                "connection_degree": None,
                "notes": None,
            }

        records = self._load_records()
        email_lower = email.lower()
        name_lower = (display_name or "").strip().lower()
        for record in records:
            record_email = record.get("email", "").strip().lower()
            record_name = record.get("name", "").strip().lower()
            if record_email and record_email == email_lower:
                return _record_to_lookup(record, matched_by="email")
            if name_lower and record_name and record_name == name_lower:
                return _record_to_lookup(record, matched_by="name")

        return {
            "dataset_available": True,
            "matched": False,
            "matched_by": None,
            "connection_degree": None,
            "notes": None,
        }

    def _load_records(self) -> list[dict[str, str]]:
        if self._records is not None:
            return self._records
        self._records = load_linkedin_records(self.dataset_path)
        return self._records


def load_linkedin_records(dataset_path: Path | None) -> list[dict[str, str]]:
    """Load and normalize a LinkedIn dataset or export file."""

    if dataset_path is None or not dataset_path.exists():
        return []
    suffix = dataset_path.suffix.lower()
    if suffix == ".json":
        raw_records = json.loads(dataset_path.read_text(encoding="utf-8"))
        if not isinstance(raw_records, list):
            return []
        return [
            _normalize_record({str(key): str(value) for key, value in record.items()})
            for record in raw_records
            if isinstance(record, dict)
        ]
    if suffix == ".csv":
        return _load_delimited_records(dataset_path, delimiter=",")
    if suffix == ".tsv":
        return _load_delimited_records(dataset_path, delimiter="\t")
    return []


def linkedin_record_identity(record: dict[str, str]) -> str:
    """Return a stable identity key for diffing LinkedIn records."""

    profile_url = (record.get("profile_url") or record.get("URL") or "").strip().lower()
    if profile_url:
        return f"url:{profile_url.rstrip('/')}"
    email = (record.get("email") or record.get("Email Address") or "").strip().lower()
    if email:
        return f"email:{email}"
    name = (record.get("name") or "").strip().lower()
    if name:
        return f"name:{name}"
    return ""


def split_linkedin_name(name: str) -> tuple[str, str]:
    """Split a display name into first and last components."""

    parts = [part.strip() for part in name.split() if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _record_to_lookup(record: dict[str, str], *, matched_by: str) -> dict[str, Any]:
    return {
        "dataset_available": True,
        "matched": True,
        "matched_by": matched_by,
        "connection_degree": record.get("connection_degree") or None,
        "notes": record.get("notes") or record.get("headline") or None,
        "profile_url": record.get("profile_url") or None,
    }


def _normalize_record(record: dict[str, str]) -> dict[str, str]:
    """Normalize raw LinkedIn exports into the connector's internal schema."""

    normalized = dict(record)
    email = (
        normalized.get("email")
        or normalized.get("Email Address")
        or normalized.get("email address")
        or ""
    ).strip()
    name = (normalized.get("name") or "").strip()
    if not name:
        first_name = (normalized.get("First Name") or normalized.get("first_name") or "").strip()
        last_name = (normalized.get("Last Name") or normalized.get("last_name") or "").strip()
        name = " ".join(part for part in (first_name, last_name) if part).strip()

    profile_url = (
        normalized.get("profile_url")
        or normalized.get("URL")
        or normalized.get("Profile URL")
        or ""
    ).strip()
    connection_degree = (normalized.get("connection_degree") or "").strip()
    if not connection_degree and (
        normalized.get("Connected On") or normalized.get("connected_on")
    ):
        connection_degree = "1st"

    notes = (normalized.get("notes") or normalized.get("headline") or "").strip()
    if not notes:
        company = (normalized.get("Company") or "").strip()
        position = (normalized.get("Position") or "").strip()
        notes = " @ ".join(part for part in (position, company) if part).strip()

    normalized["email"] = email
    normalized["name"] = name
    normalized["profile_url"] = profile_url
    normalized["connection_degree"] = connection_degree
    normalized["notes"] = notes
    return normalized


def _load_delimited_records(path: Path, *, delimiter: str) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=delimiter))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    if not rows:
        return []
    header_index = _detect_header_row(rows)
    headers = [str(value).strip() for value in rows[header_index]]
    if not any(headers):
        return []
    records: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        if not any(str(value).strip() for value in row):
            continue
        normalized_row = {
            headers[index]: str(row[index]).strip() if index < len(row) else ""
            for index in range(len(headers))
            if headers[index]
        }
        records.append(_normalize_record(normalized_row))
    return records


def _detect_header_row(rows: list[list[str]]) -> int:
    expected_headers = {
        "first name",
        "last name",
        "url",
    }
    for index, row in enumerate(rows[:5]):
        normalized = {str(value).strip().lower() for value in row if str(value).strip()}
        if expected_headers.issubset(normalized):
            return index
    return 0
