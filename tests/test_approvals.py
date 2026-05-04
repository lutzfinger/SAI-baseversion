from __future__ import annotations

from pathlib import Path

import pytest

from app.approvals.models import ApprovalRequiredError, PolicyDeniedError
from app.approvals.service import ApprovalService
from app.observability.audit import AuditLogger
from app.observability.run_store import RunStore
from app.shared.config import Settings
from app.shared.models import ApprovalStatus, PolicyDocument, PolicyMode, PolicyRule


def _approval_policy() -> PolicyDocument:
    return PolicyDocument(
        policy_id="test_approval_policy",
        version="1",
        description="Synthetic policy for approval service coverage.",
        default_mode=PolicyMode.DENY,
        rules=[
            PolicyRule(
                action="connector.gmail.modify_*",
                mode=PolicyMode.APPROVAL_REQUIRED,
                reason=(
                    "Mailbox mutation requires an explicit approval in this synthetic "
                    "test policy."
                ),
            )
        ],
        path=Path("tests/fixtures/test_approval_policy.yaml"),
    )


def test_sensitive_action_requires_approval(test_settings: Settings) -> None:
    policy = _approval_policy()
    service = ApprovalService(RunStore(test_settings.database_path), AuditLogger(test_settings))

    with pytest.raises(ApprovalRequiredError) as error:
        service.enforce(
            run_id="run_email_triage_test",
            workflow_id="email-triage",
            policy=policy,
            action="connector.gmail.modify_labels",
            actor="pytest",
            reason="Test modifying mailbox labels.",
        )

    request = error.value.request
    assert request.status is ApprovalStatus.PENDING
    assert service.list_requests(status=ApprovalStatus.PENDING)[0].request_id == request.request_id


def test_sensitive_action_can_proceed_after_explicit_approval(test_settings: Settings) -> None:
    policy = _approval_policy()
    service = ApprovalService(RunStore(test_settings.database_path), AuditLogger(test_settings))

    pending = service.request_approval(
        run_id="run_email_triage_test",
        workflow_id="email-triage",
        action="connector.gmail.modify_labels",
        reason="Operator approved label change.",
        requested_by="pytest",
    )
    approved = service.decide(
        request_id=pending.request_id,
        approved=True,
        decided_by="pytest",
        reason="Approved in test.",
    )

    result = service.enforce(
        run_id="run_email_triage_test",
        workflow_id="email-triage",
        policy=policy,
        action="connector.gmail.modify_labels",
        actor="pytest",
        reason="Proceed after approval.",
        approval_id=approved.request_id,
    )

    assert result is not None
    assert result.status is ApprovalStatus.APPROVED


def test_unlisted_sensitive_action_is_denied_by_default(test_settings: Settings) -> None:
    policy = _approval_policy()
    service = ApprovalService(RunStore(test_settings.database_path), AuditLogger(test_settings))

    with pytest.raises(PolicyDeniedError):
        service.enforce(
            run_id="run_email_triage_test",
            workflow_id="email-triage",
            policy=policy,
            action="connector.email.delete_message",
            actor="pytest",
            reason="Delete should never happen without an explicit policy rule.",
        )
