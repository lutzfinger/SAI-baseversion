"""Generic Gmail label connector for the starter repo."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.workers.email_models import (
    EmailThreadTagRequest,
    EmailThreadTagResult,
    GmailThreadLabelRequest,
    GmailThreadLabelResult,
    all_taxonomy_gmail_label_names,
)


class GmailLabelConnector:
    """Create starter labels, apply them, and optionally archive a thread."""

    def __init__(
        self,
        *,
        authenticator: GmailOAuthAuthenticator,
        user_id: str = "me",
        service: Any | None = None,
    ) -> None:
        self.authenticator = authenticator
        self.user_id = user_id
        self._service = service

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.gmail.authenticate",
                reason="Applying starter Gmail labels needs an explicit Gmail OAuth session.",
            ),
            ConnectorAction(
                action="connector.gmail.modify_labels",
                reason="Starter tagging applies bounded labels and may archive from Inbox.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        auth_summary = self.authenticator.auth_summary()
        return ConnectorDescriptor(
            component_name="connector.gmail-labels",
            source_details={
                "user_id": self.user_id,
                "label_namespace": "Starter/*",
                "scope_count": auth_summary.get("scope_count", "0"),
                "scopes": auth_summary.get("scopes", ""),
            },
        )

    def apply_thread_tags(self, request: EmailThreadTagRequest) -> EmailThreadTagResult:
        result = self.apply_thread_labels(
            GmailThreadLabelRequest(
                thread_id=request.thread_id,
                label_names=request.gmail_label_names(),
                archive_from_inbox=request.archive_from_inbox,
                clear_taxonomy_labels=True,
            )
        )
        return EmailThreadTagResult.model_validate(result.model_dump(mode="json"))

    def apply_thread_labels(self, request: GmailThreadLabelRequest) -> GmailThreadLabelResult:
        service = self._service or self.authenticator.build_service()
        label_name_to_id, created = self._ensure_labels(service, request.label_names)
        all_known_label_ids = self._list_label_name_to_id(service)
        all_known_label_ids.update(label_name_to_id)
        target_names = [name for name in request.label_names if name in label_name_to_id]
        target_ids = [label_name_to_id[name] for name in target_names]
        removed_name_to_id: dict[str, str] = {}
        if request.clear_taxonomy_labels:
            for name in all_taxonomy_gmail_label_names():
                label_id = all_known_label_ids.get(name)
                if label_id and name not in target_names:
                    removed_name_to_id[name] = label_id
        for name in request.remove_label_names:
            label_id = all_known_label_ids.get(name)
            if label_id and name not in target_names:
                removed_name_to_id[name] = label_id
        remove_label_ids = list(removed_name_to_id.values())
        removed_label_names = sorted(removed_name_to_id)
        if request.archive_from_inbox:
            remove_label_ids.append("INBOX")
            removed_label_names = sorted({*removed_label_names, "INBOX"})

        (
            service.users()
            .threads()
            .modify(
                userId=self.user_id,
                id=request.thread_id,
                body={"addLabelIds": target_ids, "removeLabelIds": remove_label_ids},
            )
            .execute()
        )
        return GmailThreadLabelResult(
            thread_id=request.thread_id,
            applied_label_names=target_names,
            applied_label_ids=target_ids,
            removed_label_names=removed_label_names,
            removed_label_ids=sorted(remove_label_ids),
            created_label_names=sorted(created),
            created_label_ids=sorted(label_name_to_id[name] for name in created),
            archived_from_inbox=request.archive_from_inbox,
        )

    def list_thread_taxonomy_labels(self, *, thread_id: str) -> list[str]:
        service = self._service or self.authenticator.build_service()
        response = service.users().labels().list(userId=self.user_id).execute()
        items = response.get("labels", [])
        taxonomy_names = set(all_taxonomy_gmail_label_names())
        label_id_to_name = {
            str(item.get("id", "")): str(item.get("name", ""))
            for item in items
            if isinstance(item, dict) and str(item.get("name", "")) in taxonomy_names
        }
        thread = (
            service.users()
            .threads()
            .get(userId=self.user_id, id=thread_id, format="metadata")
            .execute()
        )
        names: set[str] = set()
        for message in thread.get("messages", []):
            if not isinstance(message, dict):
                continue
            for label_id in message.get("labelIds", []):
                label_name = label_id_to_name.get(str(label_id), "")
                if label_name:
                    names.add(label_name)
        return sorted(names)

    def _list_label_name_to_id(self, service: Any) -> dict[str, str]:
        response = service.users().labels().list(userId=self.user_id).execute()
        items = response.get("labels", [])
        return {
            str(item.get("name", "")): str(item.get("id", ""))
            for item in items
            if isinstance(item, dict)
        }

    def _ensure_labels(
        self,
        service: Any,
        label_names: list[str],
    ) -> tuple[dict[str, str], list[str]]:
        labels_resource = service.users().labels()
        label_name_to_id = self._list_label_name_to_id(service)
        created: list[str] = []
        for label_name in label_names:
            if label_name in label_name_to_id:
                continue
            response = labels_resource.create(
                userId=self.user_id,
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
            label_name_to_id[label_name] = str(response.get("id", ""))
            created.append(label_name)
        return label_name_to_id, created
