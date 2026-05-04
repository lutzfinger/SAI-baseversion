from __future__ import annotations

from typing import Any

from app.connectors.gmail_taxonomy_labels import GmailTaxonomyLabelInspector


class _FakeAuthenticator:
    def auth_summary(self) -> dict[str, str]:
        return {
            "credential_source": "token_file",
            "scope_count": "2",
            "scopes": "gmail.readonly, gmail.modify",
        }


class _FakeExecutable:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _FakeLabelsResource:
    def list(self, **kwargs: Any) -> _FakeExecutable:
        assert kwargs == {"userId": "me"}
        return _FakeExecutable(
            {
                "labels": [
                    {"id": "lbl-friends", "name": "L1/Friends"},
                    {"id": "lbl-casual", "name": "L2/Casual"},
                    {"id": "lbl-system", "name": "INBOX"},
                ]
            }
        )


class _FakeThreadsResource:
    def get(self, **kwargs: Any) -> _FakeExecutable:
        assert kwargs == {"userId": "me", "id": "thread-123", "format": "metadata"}
        return _FakeExecutable(
            {
                "messages": [
                    {"labelIds": ["lbl-friends", "lbl-system"]},
                    {"labelIds": ["lbl-casual", "lbl-friends"]},
                    "skip-me",
                ]
            }
        )


class _FakeUsersResource:
    def labels(self) -> _FakeLabelsResource:
        return _FakeLabelsResource()

    def threads(self) -> _FakeThreadsResource:
        return _FakeThreadsResource()


class _FakeService:
    def users(self) -> _FakeUsersResource:
        return _FakeUsersResource()


def test_gmail_taxonomy_label_inspector_lists_unique_sorted_taxonomy_labels() -> None:
    inspector = GmailTaxonomyLabelInspector(
        authenticator=_FakeAuthenticator(),
        service=_FakeService(),
    )

    labels = inspector.list_thread_taxonomy_labels(thread_id="thread-123")

    assert labels == ["L1/Friends", "L2/Casual"]


def test_gmail_taxonomy_label_inspector_describe_reports_read_only_namespace() -> None:
    inspector = GmailTaxonomyLabelInspector(
        authenticator=_FakeAuthenticator(),
        user_id="primary",
        service=_FakeService(),
    )

    descriptor = inspector.describe()

    assert descriptor.component_name == "connector.gmail-taxonomy-labels"
    assert descriptor.source_details == {
        "user_id": "primary",
        "label_namespace": "L1/L2",
        "mode": "read_only",
        "credential_source": "token_file",
        "scope_count": "2",
        "scopes": "gmail.readonly, gmail.modify",
    }
