"""op_env — bootstrap `op` CLI to run in service-account mode.

Per SAI principle #7a (1Password access is service-account only — never
interactive unlock), every code path that resolves `op://` references
MUST run with `OP_SERVICE_ACCOUNT_TOKEN` set BEFORE invoking `op`. The
operator should NEVER see a 1Password unlock dialog or the macOS
"\"op\" would like to access data from other apps" prompt.

For interactive zsh shells, `~/.zshenv` populates the env var by reading
from macOS Keychain. But `launchd`, cron, IDEs, and subprocesses don't
read `.zshenv` — so the daemon would prompt every time it fetched a
secret. This module closes that gap.

Idempotent: safe to call from any number of modules at any depth.
Pure read-only — touches only the calling process's env.
"""
from __future__ import annotations

import os
import subprocess


# macOS Keychain coordinates for the 1Password service-account token.
# Matches the entry the operator's `~/.zshenv` already reads
# (`security find-generic-password -s sai-op-service-account-token
# -a "$USER" -w`). Per SAI #17 (Public ships mechanism, Private ships
# values) this would normally live in the overlay; we put it here
# because the entry naming is part of the SAI baseversion's setup
# contract (see scripts/with_1password.sh) and stays operator-agnostic
# beyond the literal account name being the local Unix user.
KEYCHAIN_SERVICE = "sai-op-service-account-token"
# Account = local Unix user (resolved at runtime below). Keeps base
# skill free of any hardcoded username.

# Per-secret keychain cache.
#
# Calling `op item get ...` from a launchd-spawned daemon triggers the
# macOS "op would like to access data from other apps" TCC prompt EVERY
# time, even with OP_SERVICE_ACCOUNT_TOKEN set — the `op` binary
# probes the 1Password desktop XPC service which is cross-app data.
# The fix: invoke `op` ONCE while the operator is at the keyboard
# (during setup), copy the resolved secret into a fresh Keychain entry,
# and have the daemon read it via `security find-generic-password`
# which does NOT trigger the TCC prompt for the user's own keychain.
#
# Keychain entry format:
#   account=$USER, service=`sai-secret-<logical-name>`
# Example: `sai-secret-anthropic` holds the Anthropic API key.
KEYCHAIN_SECRET_PREFIX = "sai-secret-"

# Set on every call regardless of whether the token was already in env.
# These force `op` into service-account mode and turn off the desktop-app
# integration dialog that fires when `op` thinks it can ask the GUI.
_DISABLE_GUI_ENV = {
    "OP_BIOMETRIC_UNLOCK_ENABLED": "false",
    "OP_CACHE": "false",
    # Force `op` to NOT consult `~/.config/op` (which can carry stale
    # signin state pointing at a non-service-account user).
    "OP_CONFIG_DIR": "/tmp/.sai-op-config-empty",
}


def ensure_sa_token() -> None:
    """Populate `OP_SERVICE_ACCOUNT_TOKEN` in the calling process's env.

    Resolution order:
      1. Already set in env → no-op (env wins)
      2. Read from macOS Keychain entry
         (account=`sai`, service=`onepassword_service_account_token`)

    Also sets `OP_BIOMETRIC_UNLOCK_ENABLED=false`, `OP_CACHE=false`, and
    points `OP_CONFIG_DIR` at an empty tmp dir. The combination guarantees
    `op` runs in pure service-account mode with no desktop interaction.

    Does NOT raise if the keychain lookup fails — the caller's next
    `op` invocation will fail with a friendly auth error instead of a
    GUI prompt. Per #6 fail-closed: ambiguity stops, never guesses.
    """
    # Always set the no-GUI env vars first.
    for k, v in _DISABLE_GUI_ENV.items():
        os.environ.setdefault(k, v)
    # Make sure the OP_CONFIG_DIR exists with 700 perms. `op` refuses
    # to read a config dir with broader permissions ("permissions are
    # too broad" error) so we lock it down.
    cfg = os.environ.get("OP_CONFIG_DIR", "/tmp/.sai-op-config-empty")
    try:
        os.makedirs(cfg, exist_ok=True)
        os.chmod(cfg, 0o700)
    except Exception:
        pass

    if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return

    # Account name = local Unix user (the `~/.zshenv` pattern). Resolve
    # via $USER first, fall back to `id -un`.
    account = os.environ.get("USER") or ""
    if not account:
        try:
            account = subprocess.run(
                ["id", "-un"], capture_output=True, text=True, timeout=3,
            ).stdout.strip()
        except Exception:
            return

    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE,
             "-a", account,
             "-w"],
            capture_output=True, text=True, timeout=5,
        )
        token = r.stdout.strip()
        if r.returncode == 0 and token:
            os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = token
    except Exception:
        # Silent — the caller's `op` invocation will raise the friendly
        # auth error if the token is unreachable.
        pass


def _account() -> str:
    """Local Unix user — the keychain account name."""
    a = os.environ.get("USER") or ""
    if a:
        return a
    try:
        return subprocess.run(
            ["id", "-un"], capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return ""


def get_cached_secret(logical_name: str) -> str | None:
    """Read a previously-cached secret from macOS Keychain.

    Returns the secret string, or None if not cached. This call does
    NOT trigger any GUI prompt — `security` accesses the user's own
    keychain entries without TCC consent dialogs.
    """
    account = _account()
    if not account:
        return None
    service = f"{KEYCHAIN_SECRET_PREFIX}{logical_name}"
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def cache_secret(logical_name: str, value: str) -> None:
    """Stash a secret in macOS Keychain so the daemon can read it later
    via `security` without invoking `op` (which prompts).

    The ACL is set to trust `/usr/bin/security` explicitly via `-T`
    flags. Without this, every read from a launchd-spawned subprocess
    triggers the "security wants to use your confidential information
    stored in ..." dialog. Per macOS TCC docs, `-T` adds an app to the
    item's "Allow these apps" list; subsequent reads from that exact
    binary do NOT prompt.

    We delete-then-recreate (rather than `-U` upsert) because `-U`
    preserves the existing ACL, defeating the point of setting a new
    one. Idempotent at the secret-VALUE level (final state is "this
    value is the only one stored under this service+account").

    Run this ONCE during interactive setup. After the value is cached,
    `op` is never invoked by the daemon.
    """
    if not value:
        raise ValueError("refusing to cache an empty secret")
    account = _account()
    if not account:
        raise RuntimeError("can't resolve local Unix user")
    service = f"{KEYCHAIN_SECRET_PREFIX}{logical_name}"

    # Delete any existing entry — its ACL may not include security.
    # Errors here are fine (no entry to delete → nothing to do).
    subprocess.run(
        ["security", "delete-generic-password",
         "-s", service, "-a", account],
        capture_output=True, text=True, timeout=5,
    )

    # Recreate with -T flags. Trust the `security` CLI itself plus
    # the Homebrew duplicate (if PATH ordering resolves there first).
    # Both binaries are signed by Apple and macOS treats their TCC
    # identity equivalently.
    trusted_apps = ["/usr/bin/security"]
    for alt in ("/opt/homebrew/bin/security", "/usr/local/bin/security"):
        if os.path.exists(alt):
            trusted_apps.append(alt)

    cmd = ["security", "add-generic-password",
           "-s", service, "-a", account,
           "-w", value]
    for app in trusted_apps:
        cmd += ["-T", app]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(
            f"keychain cache failed for {logical_name!r}: {r.stderr.strip()}"
        )


def resolve_via_op_then_cache(
    logical_name: str,
    op_item: str,
    op_vault: str,
    op_field: str = "password",
) -> str:
    """Call `op` once (interactive — operator must approve any prompt),
    then cache the result in Keychain so future reads bypass `op`.

    This is the ONLY function in the skill that's allowed to invoke
    `op` from a non-interactive context. Call from setup helpers
    while the operator is at the keyboard.
    """
    ensure_sa_token()  # set service-account env vars
    r = subprocess.run(
        ["op", "item", "get", op_item, "--vault", op_vault,
         "--reveal", "--fields", f"label={op_field}"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"op item get {op_item!r} failed: {r.stderr.strip()}"
        )
    value = r.stdout.strip()
    cache_secret(logical_name, value)
    return value


# Run on import so every module that uses `op` inherits a clean env
# without needing to remember the call.
ensure_sa_token()
