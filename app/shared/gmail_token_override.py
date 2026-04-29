"""Helpers for selecting a workflow-specific Gmail token file at runtime."""

from __future__ import annotations

import os
from pathlib import Path

from app.connectors.gmail_config import GmailConnectorPolicy
from app.control_plane.loaders import PolicyStore, WorkflowStore
from app.shared.config import Settings, get_settings


def apply_gmail_token_path_override(
    *,
    workflow_id: str,
    token_path: str | None,
    settings: Settings | None = None,
    workflow_store: WorkflowStore | None = None,
    policy_store: PolicyStore | None = None,
) -> tuple[str, str] | None:
    """Set the correct Gmail token-path env var for a Gmail-backed workflow.

    Returns ``(env_var_name, resolved_token_path)`` when an override is applied,
    otherwise ``None``.
    """

    if token_path is None or not token_path.strip():
        return None

    resolved_settings = settings or get_settings()
    resolved_workflow_store = workflow_store or WorkflowStore(resolved_settings.workflows_dir)
    resolved_policy_store = policy_store or PolicyStore(resolved_settings.policies_dir)

    workflow = resolved_workflow_store.load(f"{workflow_id}.yaml")
    if workflow.connector not in {"gmail_api", "gmail_partial_labels"}:
        raise ValueError(f"Workflow {workflow_id} does not use a Gmail-backed connector.")

    policy = resolved_policy_store.load(workflow.policy)
    gmail_policy = GmailConnectorPolicy.from_policy(policy)
    env_name = gmail_policy.token_path_env or "SAI_GMAIL_TOKEN_PATH"
    resolved_token_path = str(Path(token_path).expanduser())
    os.environ[env_name] = resolved_token_path
    return env_name, resolved_token_path
