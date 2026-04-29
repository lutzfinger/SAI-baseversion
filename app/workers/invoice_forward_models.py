"""Schemas for filing tagged invoice receipts into QuickBooks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PromptDocument
from app.tools.models import ToolExecutionRecord
from app.workers.email_models import EmailMessage

InvoiceForwardStatus = Literal[
    "not_allowlisted",
    "forwarded_to_quickbooks",
    "failed",
]


class InvoiceAllowlistDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    allowlisted: bool
    vendor_name: str | None = None
    reason: str
    matched_rule: str | None = None


class InvoiceForwardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    allowlist: InvoiceAllowlistDecision
    status: InvoiceForwardStatus
    forwarded_to_email: str | None = None
    forwarded_message_id: str | None = None
    forwarded_subject: str | None = None
    tool_records: list[ToolExecutionRecord] = Field(default_factory=list)


class InvoiceForwardArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    generated_at: datetime
    prompt_ids: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    prompt_sha256_map: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    reviewed_message_ids: list[str] = Field(default_factory=list)
    allowlisted_message_ids: list[str] = Field(default_factory=list)
    forwarded_message_ids: list[str] = Field(default_factory=list)
    items: list[InvoiceForwardItem] = Field(default_factory=list)


def build_invoice_forward_artifact(
    *,
    run_id: str,
    workflow_id: str,
    prompts: list[PromptDocument],
    runtime: dict[str, str] | None = None,
    reviewed_message_ids: list[str],
    items: list[InvoiceForwardItem],
) -> InvoiceForwardArtifact:
    return InvoiceForwardArtifact(
        run_id=run_id,
        workflow_id=workflow_id,
        generated_at=datetime.now(UTC),
        prompt_ids=[prompt.prompt_id for prompt in prompts],
        prompt_versions=[prompt.version for prompt in prompts],
        prompt_sha256_map={prompt.prompt_id: prompt.sha256 for prompt in prompts},
        runtime=runtime or {},
        reviewed_message_ids=reviewed_message_ids,
        allowlisted_message_ids=[
            item.message.message_id for item in items if item.allowlist.allowlisted
        ],
        forwarded_message_ids=[
            item.message.message_id
            for item in items
            if item.status == "forwarded_to_quickbooks"
        ],
        items=items,
    )
