"""Human-in-the-loop approval and policy enforcement service.

This module implements the approval layer from the architecture plan. It keeps
policy decisions, approval requests, and approval consumption out of worker code
so sensitive actions remain centralized and auditable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.approvals.models import ApprovalRequest, ApprovalRequiredError, PolicyDeniedError
from app.observability.audit import AuditLogger
from app.observability.run_store import RunStore
from app.shared.models import ApprovalStatus, PolicyDocument, PolicyMode
from app.shared.run_ids import new_id


class ApprovalService:
    """Own the lifecycle of approval requests and policy evaluation."""

    def __init__(
        self,
        run_store: RunStore,
        audit_logger: AuditLogger,
    ) -> None:
        self.run_store = run_store
        self.audit_logger = audit_logger

    def request_approval(
        self,
        *,
        run_id: str,
        workflow_id: str,
        action: str,
        reason: str,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Create a pending approval request and append it to the audit trail."""

        request = ApprovalRequest(
            request_id=new_id("apr"),
            run_id=run_id,
            workflow_id=workflow_id,
            action=action,
            reason=reason,
            status=ApprovalStatus.PENDING,
            requested_by=requested_by,
            requested_at=datetime.now(UTC),
            metadata=metadata or {},
        )
        self.run_store.create_approval_request(request)
        self.audit_logger.append_event(
            run_id=run_id,
            workflow_id=workflow_id,
            actor=requested_by,
            component="approvals",
            event_type="approval.requested",
            payload={
                "request_id": request.request_id,
                "action": action,
                "reason": reason,
            },
        )
        return request

    def decide(
        self,
        *,
        request_id: str,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Record a human decision on a previously requested approval."""

        current = self.run_store.get_approval_request(request_id)
        updated = current.model_copy(
            update={
                "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
                "decided_at": datetime.now(UTC),
                "decided_by": decided_by,
                "reason": reason or current.reason,
                "metadata": {**current.metadata, **(metadata or {})},
            }
        )
        self.run_store.update_approval_request(updated)
        self.audit_logger.append_event(
            run_id=updated.run_id,
            workflow_id=updated.workflow_id,
            actor=decided_by,
            component="approvals",
            event_type="approval.approved" if approved else "approval.denied",
            payload={
                "request_id": updated.request_id,
                "action": updated.action,
                "reason": updated.reason or "",
            },
        )
        return updated

    def enforce(
        self,
        *,
        run_id: str,
        workflow_id: str,
        policy: PolicyDocument,
        action: str,
        actor: str,
        reason: str,
        approval_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest | None:
        """Apply policy rules and require approval when the policy demands it.

        This "double gate" is one of the key safety properties in the original
        plan: policy decides whether an action is allowed at all, and approvals
        decide whether an operator has explicitly permitted a sensitive action.
        """

        mode = policy.mode_for(action)
        self.audit_logger.append_event(
            run_id=run_id,
            workflow_id=workflow_id,
            actor=actor,
            component="policy",
            event_type="policy.checked",
            payload={"action": action, "mode": mode.value},
        )
        if mode is PolicyMode.ALLOW:
            return None
        if mode is PolicyMode.DENY:
            # Denials are logged before the exception is raised so incident
            # review can see both the attempted action and the policy result.
            self.audit_logger.append_event(
                run_id=run_id,
                workflow_id=workflow_id,
                actor=actor,
                component="policy",
                event_type="policy.denied",
                payload={"action": action, "reason": reason},
            )
            self.audit_logger.append_event(
                run_id=run_id,
                workflow_id=workflow_id,
                actor=actor,
                component="policy",
                event_type="policy.denied.reported",
                payload={
                    "action": action,
                    "reason": reason,
                    "feedback_recorded": False,
                },
            )
            raise PolicyDeniedError(action=action, reason=reason)

        if approval_id is not None:
            # A provided approval ID is only accepted if it was explicitly
            # recorded as approved in the central approval store.
            request = self.run_store.get_approval_request(approval_id)
            if request.status is ApprovalStatus.APPROVED:
                self.audit_logger.append_event(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    actor=actor,
                    component="approvals",
                    event_type="approval.used",
                    payload={"request_id": request.request_id, "action": action},
                )
                return request

        pending = self.request_approval(
            run_id=run_id,
            workflow_id=workflow_id,
            action=action,
            reason=reason,
            requested_by=actor,
            metadata=metadata,
        )
        raise ApprovalRequiredError(pending)

    def list_requests(self, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        """Return approval requests, optionally filtered by status."""

        if status is None:
            return self.run_store.list_approval_requests()
        return self.run_store.list_approval_requests(status=status.value)
