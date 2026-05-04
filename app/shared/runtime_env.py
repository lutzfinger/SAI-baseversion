"""Runtime env loading for direct Python entrypoints.

Shell wrappers already source `~/.config/sai/runtime.env` and resolve any
`keychain://...` / `op://...` references before launching Python. Direct
invocations like `.venv/bin/python scripts/run_email_triage.py ...` bypass
that shell layer, so this module mirrors the same runtime env loading in
Python.

Two secret-reference schemes supported:

  * ``keychain://<service>/<account>`` — macOS Keychain (legacy; works
    pre-login + in containers).
  * ``op://<vault>/<item>/<field>`` — 1Password CLI (preferred per
    principle #24a; operator's master vault, no manual mirror step).

Mixed schemes per file are fine — the loader picks per line.
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
    if value.startswith("op://"):
        return _resolve_op_ref(value)
    if value.startswith("keychain://"):
        return _resolve_keychain_ref(value)
    return os.path.expanduser(os.path.expandvars(value))


def _resolve_keychain_ref(value: str) -> str | None:
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


def _resolve_op_ref(value: str) -> str | None:
    """Resolve `op://<vault>/<item>/<field>` via the 1Password CLI.

    Requires the `op` binary on PATH and an authenticated session
    (interactive `op signin` OR a service account token in
    `OP_SERVICE_ACCOUNT_TOKEN`). On failure we return None so the
    rest of the env load continues — caller's responsibility to
    detect missing required secrets.
    """

    op_bin = _find_op_binary()
    if op_bin is None:
        return None
    try:
        result = subprocess.run(
            [op_bin, "read", value],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    resolved = result.stdout.strip()
    return resolved or None


def _find_op_binary() -> str | None:
    for candidate in ("/opt/homebrew/bin/op", "/usr/local/bin/op", "op"):
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None
