from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class OnePasswordAuthStatus(BaseModel):
    """Result of a fast 1Password CLI auth preflight."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    mode: Literal[
        "service_account",
        "interactive_signed_in",
        "missing_auth",
        "timeout",
        "op_missing",
    ]
    detail: str | None = None
    timeout_seconds: int


def check_onepassword_auth(
    *,
    env_file: str | None = None,
    timeout_seconds: int = 8,
    require_service_account: bool = False,
) -> OnePasswordAuthStatus:
    return check_onepassword_run_auth(
        env_file=env_file,
        timeout_seconds=timeout_seconds,
        require_service_account=require_service_account,
    )


def check_onepassword_run_auth(
    *,
    env_file: str | None = None,
    timeout_seconds: int = 8,
    require_service_account: bool = False,
) -> OnePasswordAuthStatus:
    """Return whether the configured 1Password auth path is usable.

    We intentionally avoid probing with `op run` here because that can trigger
    a second desktop-app approval before the real wrapped command starts.
    """

    if os.getenv("OP_SERVICE_ACCOUNT_TOKEN", "").strip():
        return OnePasswordAuthStatus(
            ready=True,
            mode="service_account",
            detail="Using OP_SERVICE_ACCOUNT_TOKEN for headless 1Password access.",
            timeout_seconds=timeout_seconds,
        )

    resolved_env_file = (env_file or "").strip()
    if resolved_env_file:
        env_path = Path(resolved_env_file).expanduser()
        if not env_path.is_file():
            return OnePasswordAuthStatus(
                ready=False,
                mode="missing_auth",
                detail=f"1Password env reference file not found: {env_path}",
                timeout_seconds=timeout_seconds,
            )

    if require_service_account:
        return OnePasswordAuthStatus(
            ready=False,
            mode="missing_auth",
            detail=(
                "Headless 1Password mode requires OP_SERVICE_ACCOUNT_TOKEN to be "
                "available from runtime.env or the environment."
            ),
            timeout_seconds=timeout_seconds,
        )

    command = ["op", "whoami"]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return OnePasswordAuthStatus(
            ready=False,
            mode="op_missing",
            detail="1Password CLI ('op') is not installed or not on PATH.",
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return OnePasswordAuthStatus(
            ready=False,
            mode="timeout",
            detail=(
                "1Password CLI auth check timed out. This usually means app-integration "
                "approval is blocking or the desktop app is not responding."
            ),
            timeout_seconds=timeout_seconds,
        )

    if result.returncode == 0:
        return OnePasswordAuthStatus(
            ready=True,
            mode="interactive_signed_in",
            detail="1Password CLI is authenticated and ready for `op run`.",
            timeout_seconds=timeout_seconds,
        )

    detail = (result.stderr or result.stdout or "").strip()
    if not detail:
        detail = "1Password CLI is not currently authenticated for interactive use."
    return OnePasswordAuthStatus(
        ready=False,
        mode="missing_auth",
        detail=detail,
        timeout_seconds=timeout_seconds,
    )
