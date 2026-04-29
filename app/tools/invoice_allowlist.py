"""Deterministic allowlist checker for tagged invoice forwarding workflows."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from app.shared.config import REPO_ROOT
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.email_models import EmailMessage
from app.workers.invoice_forward_models import InvoiceAllowlistDecision


class InvoiceAllowlistTool:
    """Check whether a tagged invoice belongs to an allowed filing vendor."""

    def __init__(self, *, tool_id: str, config: dict[str, object]) -> None:
        self.tool_id = tool_id
        self.vendor_rules = _load_vendor_rules(config.get("whitelist_path"))

    def check(
        self,
        *,
        message: EmailMessage,
    ) -> tuple[InvoiceAllowlistDecision, ToolExecutionRecord]:
        sender = message.from_email.strip().lower()
        sender_name = (message.from_name or "").strip().lower()
        subject = message.subject.strip().lower()
        matched_rule: str | None = None
        vendor_name: str | None = None
        allowlisted = False
        reason = "Sender is not on the QuickBooks invoice allowlist."

        for vendor_rule in self.vendor_rules:
            rule_name = str(vendor_rule["name"])
            if sender in vendor_rule["sender_emails"]:
                allowlisted = True
                vendor_name = rule_name
                matched_rule = f"sender_email:{sender}"
                reason = f"Sender email {sender} matched allowlisted vendor {rule_name}."
                break
            if "@" in sender and sender.rsplit("@", 1)[1] in vendor_rule["sender_domains"]:
                domain = sender.rsplit("@", 1)[1]
                allowlisted = True
                vendor_name = rule_name
                matched_rule = f"sender_domain:{domain}"
                reason = f"Sender domain {domain} matched allowlisted vendor {rule_name}."
                break
            for token in vendor_rule["sender_name_contains"]:
                if token in sender_name:
                    allowlisted = True
                    vendor_name = rule_name
                    matched_rule = f"sender_name_contains:{token}"
                    reason = (
                        f"Sender name matched allowlisted vendor token {token} for {rule_name}."
                    )
                    break
            if allowlisted:
                break
            for token in vendor_rule["subject_contains"]:
                if token in subject:
                    allowlisted = True
                    vendor_name = rule_name
                    matched_rule = f"subject_contains:{token}"
                    reason = f"Subject matched allowlisted vendor token {token} for {rule_name}."
                    break
            if allowlisted:
                break

        decision = InvoiceAllowlistDecision(
            message_id=message.message_id,
            allowlisted=allowlisted,
            vendor_name=vendor_name,
            reason=reason,
            matched_rule=matched_rule,
        )
        record = ToolExecutionRecord(
            tool_id=self.tool_id,
            tool_kind="invoice_allowlist_checker",
            status=ToolExecutionStatus.COMPLETED,
            details=decision.model_dump(mode="json"),
        )
        return decision, record


def _load_vendor_rules(reference: object) -> list[dict[str, object]]:
    raw_reference = str(reference or "").strip()
    if not raw_reference:
        return []
    path = Path(raw_reference)
    resolved = path if path.is_absolute() else REPO_ROOT / path
    if not resolved.exists():
        raise FileNotFoundError(f"Invoice whitelist file not found: {resolved}")
    loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        raise ValueError("Invoice whitelist file must contain a YAML mapping.")
    vendors = loaded.get("vendors", [])
    if not isinstance(vendors, list):
        raise ValueError("Invoice whitelist file must define 'vendors' as a list.")
    rules: list[dict[str, object]] = []
    for item in vendors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        rules.append(
            {
                "name": name,
                "sender_emails": {
                    str(value).strip().lower()
                    for value in _string_list(item.get("sender_emails"))
                    if str(value).strip()
                },
                "sender_domains": {
                    str(value).strip().lower()
                    for value in _string_list(item.get("sender_domains"))
                    if str(value).strip()
                },
                "sender_name_contains": [
                    str(value).strip().lower()
                    for value in _string_list(item.get("sender_name_contains"))
                    if str(value).strip()
                ],
                "subject_contains": [
                    str(value).strip().lower()
                    for value in _string_list(item.get("subject_contains"))
                    if str(value).strip()
                ],
            }
        )
    return rules


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
