"""Launchd-backed background service management for always-on SAI helpers."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from app.shared.config import Settings

_PID_RE = re.compile(r"\bpid = (\d+)\b")
_LAST_EXIT_STATUS_RE = re.compile(r"\blast exit code = (-?\d+)\b")

RETIRED_BACKGROUND_SERVICE_LABELS: tuple[str, ...] = (
    "com.sai.api",
    "com.sai.slack-socket-mode",
)


@dataclass(frozen=True)
class BackgroundServiceSpec:
    service_key: str
    label: str
    program_arguments: tuple[str, ...]
    working_directory: Path
    standard_out_path: Path
    standard_error_path: Path
    plist_filename: str
    heartbeat_path: Path | None = None
    heartbeat_max_age_seconds: int | None = None
    healthcheck_url: str | None = None
    keep_alive: bool = True
    run_at_load: bool = True


def build_background_service_specs(settings: Settings) -> dict[str, BackgroundServiceSpec]:
    del settings
    return {}


def launch_agent_payload(spec: BackgroundServiceSpec) -> dict[str, Any]:
    return {
        "Label": spec.label,
        "ProgramArguments": list(spec.program_arguments),
        "WorkingDirectory": str(spec.working_directory),
        "RunAtLoad": spec.run_at_load,
        "KeepAlive": spec.keep_alive,
        "StandardOutPath": str(spec.standard_out_path),
        "StandardErrorPath": str(spec.standard_error_path),
    }


def write_launch_agent(spec: BackgroundServiceSpec, *, launch_agents_dir: Path) -> Path:
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    spec.standard_out_path.parent.mkdir(parents=True, exist_ok=True)
    spec.standard_error_path.parent.mkdir(parents=True, exist_ok=True)
    if spec.heartbeat_path is not None:
        spec.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents_dir / spec.plist_filename
    with plist_path.open("wb") as handle:
        plistlib.dump(launch_agent_payload(spec), handle, sort_keys=True)
    return plist_path


def ensure_launch_agent(spec: BackgroundServiceSpec, *, launch_agents_dir: Path) -> Path:
    plist_path = write_launch_agent(spec, launch_agents_dir=launch_agents_dir)
    domain = _launchctl_domain()
    _run_launchctl("bootout", f"{domain}/{spec.label}", check=False)
    _run_launchctl("bootstrap", domain, str(plist_path))
    _run_launchctl("enable", f"{domain}/{spec.label}", check=False)
    _run_launchctl("kickstart", "-k", f"{domain}/{spec.label}", check=False)
    return plist_path


def stop_launch_agent(spec: BackgroundServiceSpec, *, launch_agents_dir: Path) -> Path:
    plist_path = launch_agents_dir / spec.plist_filename
    _run_launchctl("bootout", f"{_launchctl_domain()}/{spec.label}", check=False)
    return plist_path


def remove_retired_launch_agents(*, launch_agents_dir: Path) -> dict[str, str]:
    removed: dict[str, str] = {}
    for label in RETIRED_BACKGROUND_SERVICE_LABELS:
        plist_path = launch_agents_dir / f"{label}.plist"
        _run_launchctl("bootout", f"{_launchctl_domain()}/{label}", check=False)
        if plist_path.exists():
            plist_path.unlink()
            removed[label] = str(plist_path)
    return removed


def service_status(
    spec: BackgroundServiceSpec,
    *,
    launch_agents_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    plist_path = launch_agents_dir / spec.plist_filename
    loaded, raw_state = _launchctl_print(spec.label)
    parsed = parse_launchctl_print(raw_state)
    healthy = False
    health_detail = "not_loaded"
    if loaded and spec.healthcheck_url:
        healthy = _url_healthy(spec.healthcheck_url)
        health_detail = "ok" if healthy else "unreachable"
    elif loaded and spec.heartbeat_path and spec.heartbeat_max_age_seconds:
        heartbeat_ok, heartbeat_detail = heartbeat_health(
            spec.heartbeat_path,
            max_age_seconds=spec.heartbeat_max_age_seconds,
            now=now,
        )
        healthy = heartbeat_ok
        health_detail = heartbeat_detail
    elif loaded:
        healthy = True
        health_detail = "loaded"
    return {
        "service_key": spec.service_key,
        "label": spec.label,
        "plist_path": str(plist_path),
        "plist_exists": plist_path.exists(),
        "loaded": loaded,
        "healthy": healthy,
        "health_detail": health_detail,
        "pid": parsed["pid"],
        "last_exit_status": parsed["last_exit_status"],
    }


def all_service_statuses(settings: Settings) -> dict[str, dict[str, Any]]:
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    return {
        key: service_status(spec, launch_agents_dir=launch_agents_dir)
        for key, spec in build_background_service_specs(settings).items()
    }


def parse_launchctl_print(raw_output: str) -> dict[str, int | None]:
    pid_match = _PID_RE.search(raw_output)
    exit_match = _LAST_EXIT_STATUS_RE.search(raw_output)
    return {
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit_status": int(exit_match.group(1)) if exit_match else None,
    }


def heartbeat_health(
    heartbeat_path: Path,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if not heartbeat_path.exists():
        return False, "missing"
    reference = now or datetime.now(UTC)
    age_seconds = reference.timestamp() - heartbeat_path.stat().st_mtime
    if age_seconds <= max_age_seconds:
        return True, "ok"
    return False, f"stale:{int(age_seconds)}s"


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _launchctl_print(label: str) -> tuple[bool, str]:
    domain = _launchctl_domain()
    result = _run_launchctl("print", f"{domain}/{label}", check=False)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, result.stdout


def _url_healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:  # noqa: S310 - local health check only
            return 200 <= int(getattr(response, "status", 0)) < 300
    except (OSError, URLError, ValueError):
        return False
