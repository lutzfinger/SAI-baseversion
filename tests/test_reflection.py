from __future__ import annotations

from pathlib import Path

from app.control_plane.runner import ControlPlane

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_reflection_generates_suggestion_only_report(
    control_plane: ControlPlane, tmp_path: Path
) -> None:
    source_path = tmp_path / "low_confidence_messages.json"
    source_path.write_text(
        (FIXTURES_DIR / "low_confidence_email_messages.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    control_plane.run_workflow("email-triage", source_override=str(source_path))
    report = control_plane.generate_reflection("email-triage")

    assert report["workflow_id"] == "email-triage"
    assert "summary" in report
    assert all("suggestion" in finding for finding in report["findings"])
