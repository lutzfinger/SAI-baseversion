"""
1Password CLI wrapper.

All long-lived service-account credentials live in 1Password and are read at
runtime via the `op` CLI. Nothing sensitive is ever written to disk by this
skill.

The overlay (SAI personal config) names which 1Password item holds which
credential (see config/1password-refs.yaml). The base skill stays vendor- and
account-agnostic.

Public API (atomic):
    get_field(item, field)          - read one field
    get_all_fields(item, fields)    - read several fields in one op call
    set_field(item, field, value)   - update one field (for rotated tokens)

All functions raise RuntimeError on op CLI failures so callers can fail fast.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional

# Import side effect: populates OP_SERVICE_ACCOUNT_TOKEN from macOS
# Keychain + sets OP_BIOMETRIC_UNLOCK_ENABLED=false so every `op`
# invocation below runs in service-account mode (no desktop-app
# dialog). Per SAI principle #7a.
from lib import op_env  # noqa: F401


def _check_op() -> None:
    op_env.ensure_sa_token()
    if shutil.which("op") is None:
        raise RuntimeError(
            "1Password CLI `op` not found in PATH. Install: "
            "brew install --cask 1password-cli  then sign in with `op signin`."
        )


def get_field(item: str, field: str, vault: Optional[str] = None) -> str:
    """Read one field from a 1Password item. Returns the revealed value as str."""
    _check_op()
    cmd = ["op", "item", "get", item, "--fields", field, "--reveal"]
    if vault:
        cmd += ["--vault", vault]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"op item get {item!r} field={field!r} failed:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def get_all_fields(item: str, fields: list[str], vault: Optional[str] = None) -> dict[str, str]:
    """Read multiple fields from a 1Password item in a single op call.

    Uses --format=json which returns the entire item; we filter to the
    requested fields. Faster than N separate get_field calls.
    """
    _check_op()
    cmd = ["op", "item", "get", item, "--format=json", "--reveal"]
    if vault:
        cmd += ["--vault", vault]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"op item get {item!r} failed:\n{result.stderr.strip()}")
    data = json.loads(result.stdout)
    out: dict[str, str] = {}
    for f in data.get("fields", []):
        label = f.get("label") or f.get("id")
        if label in fields:
            out[label] = f.get("value", "")
    missing = [f for f in fields if f not in out]
    if missing:
        raise RuntimeError(
            f"1Password item {item!r} is missing field(s): {missing}. "
            f"Found labels: {[f.get('label') for f in data.get('fields', [])]}"
        )
    return out


def set_field(item: str, field: str, value: str, vault: Optional[str] = None) -> None:
    """Update one field on a 1Password item. Used to write back rotated OAuth tokens."""
    _check_op()
    cmd = ["op", "item", "edit", item, f"{field}={value}"]
    if vault:
        cmd += ["--vault", vault]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"op item edit {item!r} field={field!r} failed:\n{result.stderr.strip()}"
        )
