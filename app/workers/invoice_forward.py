"""Worker for forwarding tagged invoice receipts into QuickBooks."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.gmail_auth import GmailOAuthAuthenticator
from app.connectors.gmail_send import GmailSendConnector
from app.shared.config import Settings
from app.shared.models import PolicyDocument, WorkflowToolDefinition
from app.tools.invoice_allowlist import InvoiceAllowlistTool
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.invoice_forward_models import InvoiceForwardItem


class InvoiceForwardWorker:
    """Forward allowlisted tagged invoices to the QuickBooks intake mailbox."""

    def __init__(
        self,
        *,
        settings: Settings,
        send_connector: GmailSendConnector | None = None,
    ) -> None:
        self.settings = settings
        self._send_connector = send_connector

    def file_invoices(
        self,
        *,
        messages: list[EmailMessage],
        policy: PolicyDocument,
        tool_definitions: list[WorkflowToolDefinition],
        gmail_send: GmailSendConnector | None = None,
    ) -> list[InvoiceForwardItem]:
        allowlist_definition = _tool_or_raise(tool_definitions, "invoice_allowlist_checker")
        forward_definition = _tool_or_raise(tool_definitions, "quickbooks_receipt_forwarder")
        allowlist_tool = InvoiceAllowlistTool(
            tool_id=allowlist_definition.tool_id,
            config=allowlist_definition.config,
        )
        send_connector = gmail_send or self.build_send_connector(policy=policy)
        quickbooks_email = _quickbooks_destination_or_raise(forward_definition.config)

        items: list[InvoiceForwardItem] = []
        for message in messages:
            allowlist_decision, allowlist_record = allowlist_tool.check(message=message)
            tool_records = [allowlist_record]
            if not allowlist_decision.allowlisted:
                items.append(
                    InvoiceForwardItem(
                        message=message,
                        allowlist=allowlist_decision,
                        status="not_allowlisted",
                        tool_records=tool_records,
                    )
                )
                continue
            try:
                send_result = send_connector.forward_gmail_message(
                    to_email=quickbooks_email,
                    original_message_id=message.message_id,
                    original_from_email=message.from_email,
                    original_from_name=message.from_name,
                    original_subject=message.subject,
                    original_received_at=message.received_at,
                    original_to=message.to,
                    original_cc=message.cc,
                    note=(
                        "Forwarded by SAI for QuickBooks expense filing."
                        if allowlist_decision.vendor_name is None
                        else (
                            "Forwarded by SAI for QuickBooks expense filing"
                            f" ({allowlist_decision.vendor_name})."
                        )
                    ),
                )
                tool_records.append(
                    ToolExecutionRecord(
                        tool_id=forward_definition.tool_id,
                        tool_kind=forward_definition.kind,
                        status=ToolExecutionStatus.COMPLETED,
                        details={
                            "vendor_name": allowlist_decision.vendor_name,
                            "destination_email": quickbooks_email,
                            **send_result,
                        },
                    )
                )
                items.append(
                    InvoiceForwardItem(
                        message=message,
                        allowlist=allowlist_decision,
                        status="forwarded_to_quickbooks",
                        forwarded_to_email=quickbooks_email,
                        forwarded_message_id=str(send_result.get("message_id", "")) or None,
                        forwarded_subject=str(send_result.get("subject", "")) or None,
                        tool_records=tool_records,
                    )
                )
            except Exception as error:
                error_text = str(error)
                notify_operator_directly = "attachment" in error_text.lower()
                tool_records.append(
                    ToolExecutionRecord(
                        tool_id=forward_definition.tool_id,
                        tool_kind=forward_definition.kind,
                        status=ToolExecutionStatus.FAILED,
                        details={
                            "vendor_name": allowlist_decision.vendor_name,
                            "destination_email": quickbooks_email,
                            "error": error_text,
                            "notify_operator_directly": notify_operator_directly,
                            "operator_alert_reason": (
                                "attachment_preservation_failed"
                                if notify_operator_directly
                                else None
                            ),
                        },
                    )
                )
                items.append(
                    InvoiceForwardItem(
                        message=message,
                        allowlist=allowlist_decision,
                        status="failed",
                        forwarded_to_email=quickbooks_email,
                        tool_records=tool_records,
                    )
                )
        return items

    def build_send_connector(self, *, policy: PolicyDocument) -> GmailSendConnector:
        return self._send_connector or GmailSendConnector(
            authenticator=GmailOAuthAuthenticator(settings=self.settings, policy=policy)
        )

    def required_actions(
        self,
        *,
        policy: PolicyDocument,
    ) -> list[ConnectorAction]:
        return self.build_send_connector(policy=policy).required_actions()

    def connector_descriptors(
        self,
        *,
        policy: PolicyDocument,
    ) -> list[ConnectorDescriptor]:
        return [self.build_send_connector(policy=policy).describe()]


def _tool_or_raise(
    tool_definitions: list[WorkflowToolDefinition],
    kind: str,
) -> WorkflowToolDefinition:
    for tool in tool_definitions:
        if tool.kind == kind and tool.enabled:
            return tool
    raise KeyError(f"Workflow is missing required tool kind: {kind}")


def _quickbooks_destination_or_raise(config: dict[str, Any]) -> str:
    destination = str(config.get("destination_email", "")).strip()
    if not destination:
        raise ValueError("QuickBooks destination email is required for invoice forwarding.")
    return destination
