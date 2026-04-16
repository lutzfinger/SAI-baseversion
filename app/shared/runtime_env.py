"""Runtime env loading for direct Python entrypoints.

Shell wrappers already source `~/.config/sai/runtime.env` and resolve any
`keychain://...` references before launching Python. Direct invocations like
`.venv/bin/python scripts/run_email_triage.py ...` bypass that shell layer, so
this module mirrors the same non-1Password runtime env loading in Python.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def load_runtime_env_best_effort(
    *,
    runtime_env_path: Path | None = None,
) -> None:
    """Load `runtime.env` into `os.environ` without overriding explicit env vars."""

    path = runtime_env_path or _default_runtime_env_path()
    if not path.exists():
        return

    for key, value in _parse_env_file(path).items():
        if os.getenv(key):
            continue
        resolved = _resolve_runtime_secret_ref(value)
        if resolved is None:
            continue
        os.environ[key] = resolved


def _default_runtime_env_path() -> Path:
    raw = os.getenv("PLAIN_ENV_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "sai" / "runtime.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = raw_line.partition("=")
        normalized_key = key.strip()
        if not normalized_key:
            continue
        value = raw_value.strip()
        if not value:
            parsed[normalized_key] = ""
            continue
        try:
            parsed[normalized_key] = shlex.split(value, posix=True)[0]
        except ValueError:
            parsed[normalized_key] = value.strip("'\"")
    return parsed


def _resolve_runtime_secret_ref(value: str) -> str | None:
    if not value.startswith("keychain://"):
        return os.path.expanduser(os.path.expandvars(value))
    locator = value.removeprefix("keychain://")
    service, _, account = locator.partition("/")
    if not service:
        return None
    command = ["/usr/bin/security", "find-generic-password", "-s", service, "-w"]
    if account:
        command.extend(["-a", account])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    resolved = result.stdout.strip()
    return resolved or None
