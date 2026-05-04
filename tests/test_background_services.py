from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.control_plane.background_services import (
    build_background_service_specs,
    heartbeat_health,
    parse_launchctl_print,
)
from app.shared.config import Settings


def test_background_service_specs_include_api_and_slack(test_settings: Settings) -> None:
    specs = build_background_service_specs(test_settings)

    assert specs == {}


def test_parse_launchctl_print_extracts_pid_and_exit_code() -> None:
    parsed = parse_launchctl_print(
        """
        gui/501/com.sai.api = {
            pid = 9182
            last exit code = 0
        }
        """
    )

    assert parsed["pid"] == 9182
    assert parsed["last_exit_status"] == 0


def test_heartbeat_health_reports_fresh_and_stale(tmp_path: Path) -> None:
    heartbeat_path = tmp_path / "slack_socket_mode.heartbeat"
    heartbeat_path.write_text("2026-04-01T00:00:00+00:00", encoding="utf-8")
    fresh_now = datetime.fromtimestamp(
        heartbeat_path.stat().st_mtime + 30,
        tz=UTC,
    )
    stale_now = fresh_now + timedelta(seconds=500)

    assert heartbeat_health(heartbeat_path, max_age_seconds=120, now=fresh_now) == (True, "ok")
    stale_ok, stale_detail = heartbeat_health(
        heartbeat_path,
        max_age_seconds=120,
        now=stale_now,
    )
    assert stale_ok is False
    assert stale_detail.startswith("stale:")
