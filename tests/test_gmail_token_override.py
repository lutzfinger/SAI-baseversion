from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.control_plane.loaders import PolicyStore, WorkflowStore
from app.shared.gmail_token_override import apply_gmail_token_path_override


def test_apply_gmail_token_path_override_sets_workflow_specific_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows_dir = tmp_path / "workflows"
    policies_dir = tmp_path / "policies"
    workflows_dir.mkdir()
    policies_dir.mkdir()

    (workflows_dir / "email-triage-gmail-tagging.yaml").write_text(
        "\n".join(
            [
                "workflow_id: email-triage-gmail-tagging",
                'version: "1"',
                "description: test workflow",
                "worker: email_triage",
                "connector: gmail_api",
                "policy: email_tagging.yaml",
            ]
        ),
        encoding="utf-8",
    )
    (policies_dir / "email_tagging.yaml").write_text(
        "\n".join(
            [
                "policy_id: email_tagging",
                'version: "1"',
                "default_mode: deny",
                "gmail:",
                "  allowed_env_keys:",
                "    token_path: SAI_GMAIL_TAGGING_TOKEN_PATH",
                "    client_id: SAI_GMAIL_CLIENT_ID",
                "    client_secret: SAI_GMAIL_CLIENT_SECRET",
                "  allowed_scopes:",
                "    - https://www.googleapis.com/auth/gmail.modify",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("SAI_GMAIL_TAGGING_TOKEN_PATH", raising=False)
    override = apply_gmail_token_path_override(
        workflow_id="email-triage-gmail-tagging",
        token_path="~/Library/Application Support/SAI/tokens/gmail_lutztfinger_tagging.json",
        workflow_store=WorkflowStore(workflows_dir),
        policy_store=PolicyStore(policies_dir),
    )

    assert override == (
        "SAI_GMAIL_TAGGING_TOKEN_PATH",
        str(
            Path(
                "~/Library/Application Support/SAI/tokens/gmail_lutztfinger_tagging.json"
            ).expanduser()
        ),
    )
    assert Path(os.environ["SAI_GMAIL_TAGGING_TOKEN_PATH"]) == Path(
        "~/Library/Application Support/SAI/tokens/gmail_lutztfinger_tagging.json"
    ).expanduser()


def test_apply_gmail_token_path_override_accepts_gmail_partial_label_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows_dir = tmp_path / "workflows"
    policies_dir = tmp_path / "policies"
    workflows_dir.mkdir()
    policies_dir.mkdir()

    (workflows_dir / "email-label-cleanup.yaml").write_text(
        "\n".join(
            [
                "workflow_id: email-label-cleanup",
                'version: "1"',
                "description: cleanup workflow",
                "worker: label_cleanup",
                "connector: gmail_partial_labels",
                "policy: email_label_cleanup.yaml",
            ]
        ),
        encoding="utf-8",
    )
    (policies_dir / "email_label_cleanup.yaml").write_text(
        "\n".join(
            [
                "policy_id: email_label_cleanup",
                'version: "1"',
                "default_mode: deny",
                "gmail:",
                "  allowed_env_keys:",
                "    token_path: SAI_GMAIL_TAGGING_TOKEN_PATH",
                "    client_id: SAI_GMAIL_CLIENT_ID",
                "    client_secret: SAI_GMAIL_CLIENT_SECRET",
                "  allowed_scopes:",
                "    - https://www.googleapis.com/auth/gmail.modify",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("SAI_GMAIL_TAGGING_TOKEN_PATH", raising=False)
    override = apply_gmail_token_path_override(
        workflow_id="email-label-cleanup",
        token_path="~/Library/Application Support/SAI/tokens/gmail_cleanup.json",
        workflow_store=WorkflowStore(workflows_dir),
        policy_store=PolicyStore(policies_dir),
    )

    assert override == (
        "SAI_GMAIL_TAGGING_TOKEN_PATH",
        str(Path("~/Library/Application Support/SAI/tokens/gmail_cleanup.json").expanduser()),
    )


def test_apply_gmail_token_path_override_rejects_non_gmail_workflow(
    tmp_path: Path,
) -> None:
    workflows_dir = tmp_path / "workflows"
    policies_dir = tmp_path / "policies"
    workflows_dir.mkdir()
    policies_dir.mkdir()

    (workflows_dir / "email-triage.yaml").write_text(
        "\n".join(
            [
                "workflow_id: email-triage",
                'version: "1"',
                "description: local workflow",
                "worker: email_triage",
                "connector: local_email_fixture",
                "policy: email_triage.yaml",
                "sample_source: sample.json",
            ]
        ),
        encoding="utf-8",
    )
    (policies_dir / "email_triage.yaml").write_text(
        "\n".join(
            [
                "policy_id: email_triage",
                'version: "1"',
                "default_mode: deny",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not use a Gmail-backed connector"):
        apply_gmail_token_path_override(
            workflow_id="email-triage",
            token_path="~/tmp/token.json",
            workflow_store=WorkflowStore(workflows_dir),
            policy_store=PolicyStore(policies_dir),
        )
