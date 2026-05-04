from __future__ import annotations

import pytest

from app.control_plane.runner import ControlPlane
from app.shared.config import Settings


def test_workflow_rejects_prompt_hash_mismatch(test_settings: Settings) -> None:
    lock_path = test_settings.prompts_dir / "prompt-locks.yaml"
    original = lock_path.read_text(encoding="utf-8")
    lock_path.write_text(
        original.replace(
            "e1d4fa082d079b747a6d22c03c6be8e44b42fa0b4aac555ffe8c6e51364d44c0",
            "deadbeef",
            1,
        ),
        encoding="utf-8",
    )

    control_plane = ControlPlane(test_settings)

    with pytest.raises(ValueError, match="Prompt hash mismatch"):
        control_plane.run_workflow("email-triage")
