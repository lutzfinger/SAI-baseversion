from __future__ import annotations

import json

from app.control_plane.runner import ControlPlane
from app.shared.config import Settings


def test_audit_log_supports_replay(
    control_plane: ControlPlane, test_settings: Settings
) -> None:
    result = control_plane.run_workflow("email-triage")
    detail = control_plane.get_run_detail(result.run_id)

    assert detail["replay"]["run_id"] == result.run_id
    assert detail["replay"]["event_count"] == len(detail["events"])
    assert detail["events"][0]["event_type"] == "workflow.started"
    assert detail["events"][-1]["event_type"] == "workflow.completed"

    lines = test_settings.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(line)["run_id"] == result.run_id for line in lines)
    assert test_settings.graph_checkpoint_path.exists()
