from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.models import ApprovalStatus


class ApprovalRequest(BaseModel):
    request_id: str
    run_id: str
    workflow_id: str
    action: str
    reason: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDeniedError(Exception):
    def __init__(self, *, action: str, reason: str | None = None) -> None:
        self.action = action
        self.reason = reason
        message = f"Action denied by policy: {action}"
        if reason:
            message = f"{message} ({reason})"
        super().__init__(message)


class ApprovalRequiredError(Exception):
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        super().__init__(f"Approval required for action {request.action}")
