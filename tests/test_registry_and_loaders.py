from __future__ import annotations

import hashlib

from app.control_plane.loaders import PromptLockStore, PromptStore, WorkflowStore
from app.shared.config import Settings
from app.shared.registry import list_effect_classes, list_task_kinds
from app.tools.task_assistant import build_safe_workflow_catalog


def test_prompt_locks_match_current_files(starter_settings: Settings) -> None:
    prompt_store = PromptStore(starter_settings.prompts_dir)
    prompt_locks = PromptLockStore(starter_settings.prompts_dir).load()

    for reference, expected_sha in prompt_locks.items():
        prompt = prompt_store.load(reference)
        actual_sha = hashlib.sha256(prompt.path.read_bytes()).hexdigest()
        assert prompt.sha256 == actual_sha
        assert actual_sha == expected_sha


def test_workflow_catalog_and_registry_are_starter_scoped(starter_settings: Settings) -> None:
    workflow_store = WorkflowStore(starter_settings.workflows_dir)
    workflows = workflow_store.list_workflows()

    assert {workflow.workflow_id for workflow in workflows} == {
        "newsletter-identification-gmail",
        "newsletter-identification-gmail-tagging",
        "starter-email-interaction",
    }
    assert {task_kind.kind for task_kind in list_task_kinds()} == {
        "newsletter_identification",
        "workflow_suggestion",
        "workflow_execution",
    }
    assert {effect.name for effect in list_effect_classes()} == {
        "read_only",
        "mailbox_label_mutation",
        "email_reply",
        "slack_post",
    }

    catalog = build_safe_workflow_catalog(workflows)
    assert {entry["workflow_id"] for entry in catalog} == {
        "newsletter-identification-gmail",
        "newsletter-identification-gmail-tagging",
    }
