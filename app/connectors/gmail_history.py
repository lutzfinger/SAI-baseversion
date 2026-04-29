"""Gmail history and draft helpers for meeting-decision workflows."""

from __future__ import annotations

import base64
import os
from email.message import EmailMessage as MimeEmailMessage
from pathlib import Path
from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator


class GmailHistoryConnector:
    """Summarize prior Gmail contact history and create drafts."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        extra_authenticators: list[GmailOAuthAuthenticator] | None = None,
        user_id: str = "me",
        max_history_results: int = 25,
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.extra_authenticators = extra_authenticators or []
        self.user_id = user_id
        self.max_history_results = max_history_results
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.read_history",
                reason="Meeting decisions use prior Gmail contact history.",
            ),
            ConnectorAction(
                action="connector.gmail.create_draft",
                reason="Phase 1 writes drafts only and never sends automatically.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        extra_accounts = [
            authenticator.auth_summary().get("account", "")
            for authenticator in self.extra_authenticators
        ]
        return ConnectorDescriptor(
            component_name="connector.gmail-history",
            source_details={
                "user_id": self.user_id,
                "max_history_results": self.max_history_results,
                "credential_source": auth_summary.get(
                    "credential_source",
                    "interactive_browser_flow",
                ),
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
                "mailbox_count": 1 + len(self.extra_authenticators),
                "extra_accounts": [account for account in extra_accounts if account],
            },
        )

    def summarize_contact(
        self,
        *,
        contact_email: str,
        calendar_link: str,
    ) -> dict[str, Any]:
        mailbox_summaries: list[dict[str, Any]] = []
        total_inbound = 0
        total_outbound = 0
        total_unique_ids: set[str] = set()
        sent_calendar_link = False
        for authenticator in [self.authenticator, *self.extra_authenticators]:
            service = self._service if authenticator is self.authenticator else None
            history_summary = self._summarize_contact_in_one_mailbox(
                authenticator=authenticator,
                service=service,
                contact_email=contact_email,
                calendar_link=calendar_link,
            )
            mailbox_summaries.append(history_summary)
            total_inbound += int(history_summary["prior_inbound_count"])
            total_outbound += int(history_summary["prior_outbound_count"])
            total_unique_ids.update(history_summary["message_ids"])
            sent_calendar_link = sent_calendar_link or bool(
                history_summary["has_sent_calendar_link_before"]
            )
        return {
            "contact_email": contact_email,
            "prior_inbound_count": total_inbound,
            "prior_outbound_count": total_outbound,
            "prior_total_count": len(total_unique_ids),
            "has_prior_contact": bool(total_inbound or total_outbound),
            "has_sent_calendar_link_before": sent_calendar_link,
            "mailbox_summaries": [
                {key: value for key, value in summary.items() if key != "message_ids"}
                for summary in mailbox_summaries
            ],
        }

    def summarize_meeting_evidence(
        self,
        *,
        contact_email: str,
        lookback_days: int = 365,
        contact_name: str | None = None,
    ) -> dict[str, Any]:
        mailbox_summaries: list[dict[str, Any]] = []
        positive_ids: set[str] = set()
        tentative_ids: set[str] = set()
        query_terms = [contact_email]
        if contact_name and contact_name.strip():
            query_terms.append(contact_name.strip())

        for authenticator in [self.authenticator, *self.extra_authenticators]:
            service = self._service if authenticator is self.authenticator else None
            mailbox_summary = self._summarize_meeting_evidence_in_one_mailbox(
                authenticator=authenticator,
                service=service,
                query_terms=query_terms,
                lookback_days=lookback_days,
            )
            mailbox_summaries.append(mailbox_summary)
            positive_ids.update(mailbox_summary["positive_message_ids"])
            tentative_ids.update(mailbox_summary["tentative_message_ids"])

        prior_count = len(positive_ids)
        return {
            "contact_email": contact_email,
            "lookback_days": lookback_days,
            "prior_meeting_count": prior_count,
            "meetings_in_last_12_months": prior_count,
            "upcoming_meeting_count": 0,
            "has_prior_meeting": prior_count > 0,
            "has_met_in_last_12_months": prior_count > 0,
            "met_before_in_last_12_months": prior_count > 0,
            "last_meeting_at": None,
            "source": "gmail_fallback",
            "meeting_count_is_inferred": True,
            "tentative_meeting_signal_count": len(tentative_ids),
            "mailbox_summaries": [
                {
                    key: value
                    for key, value in summary.items()
                    if key not in {"positive_message_ids", "tentative_message_ids"}
                }
                for summary in mailbox_summaries
            ],
        }

    def _summarize_contact_in_one_mailbox(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        service: Any | None,
        contact_email: str,
        calendar_link: str,
    ) -> dict[str, Any]:
        active_service = service or authenticator.build_service()
        inbound_query = f"from:{contact_email}"
        outbound_query = f"to:{contact_email} from:me"
        prior_inbound = _list_message_ids(
            service=active_service,
            user_id=self.user_id,
            query=inbound_query,
            max_results=self.max_history_results,
        )
        prior_outbound = _list_message_ids(
            service=active_service,
            user_id=self.user_id,
            query=outbound_query,
            max_results=self.max_history_results,
        )
        prior_calendar_link = _list_message_ids(
            service=active_service,
            user_id=self.user_id,
            query=f'{outbound_query} "{calendar_link}"',
            max_results=self.max_history_results,
        )
        auth_summary = authenticator.auth_summary()
        account = auth_summary.get("account", "")
        return {
            "mailbox_account": account,
            "token_path": auth_summary.get("token_path", ""),
            "prior_inbound_count": len(prior_inbound),
            "prior_outbound_count": len(prior_outbound),
            "prior_total_count": len(set(prior_inbound + prior_outbound)),
            "has_prior_contact": bool(prior_inbound or prior_outbound),
            "has_sent_calendar_link_before": bool(prior_calendar_link),
            "message_ids": sorted(set(prior_inbound + prior_outbound)),
        }

    def _summarize_meeting_evidence_in_one_mailbox(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        service: Any | None,
        query_terms: list[str],
        lookback_days: int,
    ) -> dict[str, Any]:
        active_service = service or authenticator.build_service()
        window = f"newer_than:{lookback_days}d"
        positive_queries = [
            f'{window} "{term}" "Appointment booked:"'
            for term in query_terms
        ] + [
            f'{window} "{term}" "Accepted:"'
            for term in query_terms
        ]
        tentative_queries = [
            f'{window} "{term}" "Invitation:"'
            for term in query_terms
        ] + [
            f'{window} "{term}" "Appointment canceled:"'
            for term in query_terms
        ]
        positive_ids = _list_message_ids_for_queries(
            service=active_service,
            user_id=self.user_id,
            queries=positive_queries,
            max_results=self.max_history_results,
        )
        tentative_ids = _list_message_ids_for_queries(
            service=active_service,
            user_id=self.user_id,
            queries=tentative_queries,
            max_results=self.max_history_results,
        )
        auth_summary = authenticator.auth_summary()
        return {
            "mailbox_account": auth_summary.get("account", ""),
            "token_path": auth_summary.get("token_path", ""),
            "positive_signal_count": len(positive_ids),
            "tentative_signal_count": len(tentative_ids),
            "queries_used": positive_queries + tentative_queries,
            "positive_message_ids": sorted(positive_ids),
            "tentative_message_ids": sorted(tentative_ids),
        }

    def create_draft(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        service = self._service or self.authenticator.build_service()
        mime_message = MimeEmailMessage()
        mime_message["To"] = to_email
        mime_message["Subject"] = subject
        mime_message.set_content(body)
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        response = (
            service.users()
            .drafts()
            .create(userId=self.user_id, body=payload)
            .execute()
        )
        return {
            "draft_id": str(response.get("id", "")),
            "thread_id": str(response.get("message", {}).get("threadId", thread_id or "")),
        }


def parse_extra_gmail_token_paths(raw_value: str | None) -> list[Path]:
    if raw_value is None:
        return []
    normalized = raw_value.replace(os.pathsep, "\n").replace(",", "\n")
    paths = [Path(part.strip()).expanduser() for part in normalized.splitlines() if part.strip()]
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _list_message_ids(
    *,
    service: Any,
    user_id: str,
    query: str,
    max_results: int,
) -> list[str]:
    response = (
        service.users()
        .messages()
        .list(
            userId=user_id,
            q=query,
            maxResults=max_results,
            includeSpamTrash=False,
        )
        .execute()
    )
    items = response.get("messages", [])
    if not isinstance(items, list):
        return []
    return [str(item.get("id", "")) for item in items if isinstance(item, dict)]


def _list_message_ids_for_queries(
    *,
    service: Any,
    user_id: str,
    queries: list[str],
    max_results: int,
) -> set[str]:
    message_ids: set[str] = set()
    for query in queries:
        message_ids.update(
            _list_message_ids(
                service=service,
                user_id=user_id,
                query=query,
                max_results=max_results,
            )
        )
    return message_ids
