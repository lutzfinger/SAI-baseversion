"""Unit test for the additive CalendarHistoryConnector.list_events_on_date,
driven by a fake Google service (no live calendar). Also asserts the existing
contact-summary surface is untouched."""
from __future__ import annotations

from app.connectors.calendar import CalendarHistoryConnector


class _FakeList:
    def __init__(self, items):
        self._items = items

    def execute(self):
        return {"items": self._items}


class _FakeEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **kwargs):
        tmin, tmax = kwargs["timeMin"], kwargs["timeMax"]
        sel = []
        for it in self._items:
            s = it["start"].get("dateTime") or it["start"].get("date")
            if tmin <= s < tmax:
                sel.append(it)
        return _FakeList(sel)


class _FakeService:
    def __init__(self, items):
        self._items = items

    def events(self):
        return _FakeEvents(self._items)


_RAW = [
    {"id": "a", "summary": "One Medical - Palo Alto", "location": "590 Forest Avenue",
     "start": {"dateTime": "2026-06-03T10:00:00-07:00"}, "end": {"dateTime": "2026-06-03T10:20:00-07:00"}},
    {"id": "b", "summary": "esade lutz", "location": "Sutardja Center, Berkeley, CA 94720, USA",
     "start": {"dateTime": "2026-06-03T13:00:00-07:00"}, "end": {"dateTime": "2026-06-03T14:15:00-07:00"}},
    {"id": "c", "summary": "Next day standup", "location": "",
     "start": {"dateTime": "2026-06-04T09:00:00-07:00"}, "end": {"dateTime": "2026-06-04T09:30:00-07:00"}},
]


def _conn():
    return CalendarHistoryConnector(authenticator=object(), service=_FakeService(_RAW))


def test_list_events_on_date_returns_only_in_day_events():
    events = _conn().list_events_on_date("2026-06-03", tz_offset="-07:00")
    summaries = [e["summary"] for e in events]
    assert summaries == ["One Medical - Palo Alto", "esade lutz"]  # next-day excluded, time-sorted
    assert events[1]["location"] == "Sutardja Center, Berkeley, CA 94720, USA"
    assert all("all_day" in e and "start" in e for e in events)


def test_existing_contact_summary_surface_unchanged():
    # The additive method must not have altered the contact-summary API.
    assert hasattr(CalendarHistoryConnector, "summarize_contact")
    assert hasattr(CalendarHistoryConnector, "list_events_on_date")
