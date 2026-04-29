"""Read-only Google Calendar history enrichment connector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.calendar_auth import CalendarOAuthAuthenticator

if TYPE_CHECKING:
    from app.connectors.gmail_history import GmailHistoryConnector


class CalendarHistoryConnector:
    """Summarize prior and upcoming calendar events for one contact."""

    def __init__(
        self,
        *,
        authenticator: CalendarOAuthAuthenticator,
        calendar_id: str = "primary",
        lookback_days: int = 365,
        max_results: int = 250,
        search_all_calendars: bool = False,
        gmail_history: GmailHistoryConnector | None = None,
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.calendar_id = calendar_id
        self.lookback_days = lookback_days
        self.max_results = max_results
        self.search_all_calendars = search_all_calendars
        self.gmail_history = gmail_history
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.calendar.authenticate",
                reason="Calendar enrichment uses explicit local OAuth on this machine.",
            ),
            ConnectorAction(
                action="connector.calendar.read_events",
                reason="Meeting decisions use prior and upcoming calendar history.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.calendar-api",
            source_details={
                "calendar_id": self.calendar_id,
                "lookback_days": self.lookback_days,
                "max_results": self.max_results,
                "search_all_calendars": self.search_all_calendars,
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def summarize_contact(
        self,
        *,
        contact_email: str,
        lookback_days: int | None = None,
        contact_name: str | None = None,
    ) -> dict[str, Any]:
        effective_lookback_days = lookback_days or self.lookback_days
        contact_email = contact_email.lower()
        try:
            service = self._service or self.authenticator.build_service()
            start = datetime.now(UTC) - timedelta(days=effective_lookback_days)
            calendar_ids = self._calendar_ids(service)
            query_terms = _query_terms(
                contact_email=contact_email,
                contact_name=contact_name,
            )

            prior_count, upcoming_count, last_meeting_at = self._summarize_matching_events(
                service=service,
                calendar_ids=calendar_ids,
                time_min=start.isoformat(),
                contact_email=contact_email,
                query_terms=query_terms,
            )

            # If the targeted Calendar search does not find anything, fall back
            # to a full scan so we keep the previous correctness behavior.
            if prior_count == 0 and upcoming_count == 0 and query_terms:
                prior_count, upcoming_count, last_meeting_at = self._summarize_matching_events(
                    service=service,
                    calendar_ids=calendar_ids,
                    time_min=start.isoformat(),
                    contact_email=contact_email,
                    query_terms=[None],
                )

            return {
                "contact_email": contact_email,
                "lookback_days": effective_lookback_days,
                "prior_meeting_count": prior_count,
                "meetings_in_last_12_months": prior_count,
                "upcoming_meeting_count": upcoming_count,
                "has_prior_meeting": prior_count > 0,
                "has_met_in_last_12_months": prior_count > 0,
                "met_before_in_last_12_months": prior_count > 0,
                "last_meeting_at": last_meeting_at,
                "source": "calendar",
            }
        except Exception as exc:
            if self.gmail_history is None:
                raise
            summary = self.gmail_history.summarize_meeting_evidence(
                contact_email=contact_email,
                lookback_days=effective_lookback_days,
                contact_name=contact_name,
            )
            summary["calendar_error"] = str(exc)
            return summary

    def _summarize_matching_events(
        self,
        *,
        service: Any,
        calendar_ids: list[str],
        time_min: str,
        contact_email: str,
        query_terms: Sequence[str | None],
    ) -> tuple[int, int, str | None]:
        prior_count = 0
        upcoming_count = 0
        last_meeting_at: str | None = None
        now = datetime.now(UTC)
        seen_keys: set[tuple[str, str]] = set()

        for calendar_id in calendar_ids:
            for query_term in query_terms:
                for item in self._iter_events(
                    service,
                    calendar_id=calendar_id,
                    time_min=time_min,
                    query=query_term,
                ):
                    event_key = (calendar_id, _event_dedupe_key(item))
                    if event_key in seen_keys:
                        continue
                    seen_keys.add(event_key)

                    attendees = item.get("attendees", [])
                    if not isinstance(attendees, list):
                        attendees = []
                    matched = any(
                        isinstance(attendee, dict)
                        and str(attendee.get("email", "")).lower() == contact_email
                        for attendee in attendees
                    )
                    if not matched:
                        organizer = item.get("organizer", {})
                        matched = isinstance(organizer, dict) and str(
                            organizer.get("email", "")
                        ).lower() == contact_email
                    if not matched:
                        continue
                    event_start = _parse_event_start(item.get("start"))
                    if event_start is None:
                        continue
                    if event_start <= now:
                        prior_count += 1
                        if last_meeting_at is None or event_start.isoformat() > last_meeting_at:
                            last_meeting_at = event_start.isoformat()
                    else:
                        upcoming_count += 1

        return prior_count, upcoming_count, last_meeting_at

    def _calendar_ids(self, service: Any) -> list[str]:
        if not self.search_all_calendars:
            return [self.calendar_id]

        try:
            calendar_list_resource = service.calendarList()
        except AttributeError:
            return [self.calendar_id]

        calendar_ids: list[str] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {}
            if page_token:
                kwargs["pageToken"] = page_token
            response = cast_to_mapping(calendar_list_resource.list(**kwargs).execute())
            items = response.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    calendar_id = str(item.get("id", "")).strip()
                    if calendar_id:
                        calendar_ids.append(calendar_id)
            raw_page_token = response.get("nextPageToken")
            if not isinstance(raw_page_token, str) or not raw_page_token.strip():
                break
            page_token = raw_page_token
        return calendar_ids or [self.calendar_id]

    def _iter_events(
        self,
        service: Any,
        *,
        calendar_id: str,
        time_min: str,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": time_min,
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": self.max_results,
            }
            if query:
                kwargs["q"] = query
            if page_token:
                kwargs["pageToken"] = page_token
            response = cast_to_mapping(service.events().list(**kwargs).execute())
            raw_items = response.get("items", [])
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, dict):
                        items.append(item)
            raw_page_token = response.get("nextPageToken")
            if not isinstance(raw_page_token, str) or not raw_page_token.strip():
                break
            page_token = raw_page_token
        return items


def _parse_event_start(payload: object) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("dateTime") or payload.get("date")
    if raw is None:
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def cast_to_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _query_terms(*, contact_email: str, contact_name: str | None) -> list[str]:
    terms: list[str] = []
    for candidate in [contact_email, contact_name]:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text and text not in terms:
            terms.append(text)
    return terms


def _event_dedupe_key(item: dict[str, Any]) -> str:
    identifier = str(item.get("id", "")).strip()
    if identifier:
        return identifier
    summary = str(item.get("summary", "")).strip()
    start = cast_to_mapping(item.get("start"))
    start_value = str(start.get("dateTime") or start.get("date") or "").strip()
    organizer = cast_to_mapping(item.get("organizer"))
    organizer_email = str(organizer.get("email", "")).strip().lower()
    return "|".join([summary, start_value, organizer_email])
