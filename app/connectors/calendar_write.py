"""Controlled Google Calendar write connector for governed travel operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.calendar_auth import CalendarOAuthAuthenticator


class CalendarWriteConnector:
    """Search, create, and delete Calendar events with explicit policy gating."""

    def __init__(
        self,
        *,
        authenticator: CalendarOAuthAuthenticator,
        calendar_id: str = "primary",
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.calendar_id = calendar_id
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.calendar.authenticate",
                reason="Travel execution needs an explicit Calendar OAuth session.",
            ),
            ConnectorAction(
                action="connector.calendar.read_events",
                reason="Travel execution must read Calendar events before changing them.",
            ),
            ConnectorAction(
                action="connector.calendar.write_events",
                reason="Travel execution creates and deletes Calendar events after approval.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.calendar-write",
            source_details={
                "calendar_id": self.calendar_id,
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def list_events(
        self,
        *,
        time_min: str,
        time_max: str,
        query: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        service = self._service or self.authenticator.build_service()
        response = (
            service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                q=query,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        items = response.get("items", [])
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def create_event(
        self,
        *,
        summary: str,
        description: str,
        start_datetime: str,
        end_datetime: str,
        timezone: str,
        location: str | None = None,
    ) -> dict[str, Any]:
        service = self._service or self.authenticator.build_service()
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_datetime, "timeZone": timezone},
            "end": {"dateTime": end_datetime, "timeZone": timezone},
        }
        if location:
            body["location"] = location
        created = service.events().insert(calendarId=self.calendar_id, body=body).execute()
        return {
            "event_id": str(created.get("id", "")),
            "html_link": str(created.get("htmlLink", "")),
            "summary": str(created.get("summary", summary)),
        }

    def delete_event(self, *, event_id: str) -> dict[str, Any]:
        service = self._service or self.authenticator.build_service()
        service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        return {"event_id": event_id, "deleted": True}

    def latest_location_before(
        self,
        *,
        before_datetime: datetime,
        max_results: int = 20,
    ) -> str | None:
        window_start = before_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        items = self.list_events(
            time_min=window_start.isoformat(),
            time_max=before_datetime.isoformat(),
            max_results=max_results,
        )
        latest_location: str | None = None
        latest_end: str | None = None
        for item in items:
            end_payload = item.get("end", {})
            if not isinstance(end_payload, dict):
                continue
            end_value = str(end_payload.get("dateTime", "")).strip()
            location = str(item.get("location", "")).strip()
            if not end_value or not location:
                continue
            if latest_end is None or end_value > latest_end:
                latest_end = end_value
                latest_location = location
        return latest_location
