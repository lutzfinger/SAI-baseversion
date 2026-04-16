"""Helpers for the starter email-native workflow suggestion lane."""

from __future__ import annotations

from app.shared.models import WorkflowDefinition


def build_safe_workflow_catalog(workflows: list[WorkflowDefinition]) -> list[dict[str, object]]:
    """Render the tiny starter workflow catalog the email planner can reason over."""

    entries: list[dict[str, object]] = []
    for workflow in workflows:
        if workflow.workflow_id == "starter-email-interaction":
            continue
        entries.append(
            {
                "workflow_id": workflow.workflow_id,
                "description": workflow.description,
                "connector": workflow.connector,
                "tags": list(workflow.tags),
            }
        )
    return entries
