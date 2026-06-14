"""Operator identity resolution — public mechanism, no operator PII.

The real operator addresses are NOT hardcoded here. They are resolved at
runtime, in priority order:

  1. the private overlay the daemon already loads
     (``config/identity.yaml`` -> ``email.*``; see runner.load_overlay), then
  2. environment variables (``SAI_OPERATOR_CC_ADDRESS`` /
     ``SAI_OPERATOR_SELF_ADDRESSES``), then
  3. ``example.com`` placeholders.

This keeps the public repo free of operator identity (PRINCIPLES.md #17,
boundary_check) while preserving live behavior: the email-listen daemon
passes its loaded overlay, so it keeps using the real configured addresses.
"""
from __future__ import annotations

import os

_DEFAULT_CC = "hello@example.com"
_DEFAULT_SELF: tuple[str, ...] = ("hello@example.com",)


def cc_address(overlay: dict | None = None) -> str:
    """The address to CC on operator-facing invoices."""
    email = (overlay or {}).get("email") or {}
    return (
        email.get("cc_address")
        or email.get("operator_email")
        or os.getenv("SAI_OPERATOR_CC_ADDRESS")
        or _DEFAULT_CC
    )


def self_addresses(overlay: dict | None = None) -> list[str]:
    """Operator's own addresses, e.g. to exclude from recipient searches."""
    email = (overlay or {}).get("email") or {}
    vals = [
        email.get("operator_email"),
        email.get("from_address"),
        email.get("trigger_address"),
    ]
    vals = [v for v in vals if v]
    if not vals:
        env = os.getenv("SAI_OPERATOR_SELF_ADDRESSES", "")
        vals = [a.strip() for a in env.split(",") if a.strip()]
    return vals or list(_DEFAULT_SELF)
