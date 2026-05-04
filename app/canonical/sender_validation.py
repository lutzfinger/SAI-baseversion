"""Sender validation (PRINCIPLES.md §6 fail-closed; §17 mechanism in public).

Hardened input guard for any skill that triggers on inbound email
and acts on the sender. Catches:

  * Forwarded mail — operator's own address as From
  * Reply-To redirection — From and Reply-To disagree on domain
  * Missing/malformed From
  * Domain not in the allowed-domains set

Allowed-domains + own-addresses come from
``config/sender_validation.yaml`` (private overlay; gitignored).

Per #6 every check fails closed: if data is missing, ambiguous, or
control-char-poisoned, reject. Friendly verdict goes to caller
which decides how to escalate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.canonical.patterns import CONTROL_CHARS_RE as _CONTROL_CHARS_RE
from app.canonical.patterns import EMAIL_RE as _EMAIL_RE
from app.shared.config import REPO_ROOT


SENDER_VALIDATION_PATH: Path = REPO_ROOT / "config" / "sender_validation.yaml"


class SenderValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    own_addresses: list[str] = Field(
        default_factory=list,
        description="Operator's own email addresses (lowercase). If "
                    "From matches, treat as forwarded mail.",
    )
    allowed_from_domains: list[str] = Field(
        default_factory=list,
        description="Lowercase domain suffixes (e.g. 'cornell.edu') "
                    "the From address MUST end with.",
    )
    allowed_from_addresses: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-skill test-fixture allowlist. Keyed by workflow_id. "
            "Each value is a list of lowercase addresses that bypass "
            "the domain check FOR THAT SKILL ONLY. The forward + "
            "reply-to consistency checks still apply.\n"
            "Caller must pass workflow_id to validate_sender(); without "
            "it, no per-address bypass applies (fail closed per #6)."
        ),
    )


@lru_cache(maxsize=1)
def _config() -> SenderValidationConfig:
    if not SENDER_VALIDATION_PATH.exists():
        return SenderValidationConfig()
    raw = yaml.safe_load(SENDER_VALIDATION_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return SenderValidationConfig()
    own = [str(x).lower().strip() for x in (raw.get("own_addresses") or [])]
    allowed = [
        str(x).lower().strip().lstrip("@")
        for x in (raw.get("allowed_from_domains") or [])
    ]
    raw_addrs = raw.get("allowed_from_addresses") or {}
    # Defensive: support legacy flat-list format with a clear error.
    if isinstance(raw_addrs, list):
        raise ValueError(
            "config/sender_validation.yaml: allowed_from_addresses "
            "must be a dict keyed by workflow_id (was a flat list — "
            "migrate the entries under their owning skill's id)."
        )
    if not isinstance(raw_addrs, dict):
        raw_addrs = {}
    addrs_by_skill: dict[str, list[str]] = {
        str(workflow_id): [str(a).lower().strip() for a in (addrs or [])]
        for workflow_id, addrs in raw_addrs.items()
    }
    return SenderValidationConfig(
        own_addresses=own,
        allowed_from_domains=allowed,
        allowed_from_addresses=addrs_by_skill,
    )


def reload() -> None:
    _config.cache_clear()


@dataclass
class SenderVerdict:
    """Result of a sender validation pass."""

    accepted: bool
    reason: str
    normalized_from: Optional[str] = None


def _extract_address(raw: str) -> Optional[str]:
    """Pull the bare address from `Display Name <addr@host>` style."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(r"<([^>]+)>", raw)
    if m:
        candidate = m.group(1).strip()
    else:
        candidate = raw
    candidate = candidate.lower()
    if _CONTROL_CHARS_RE.search(candidate):
        return None
    if not _EMAIL_RE.match(candidate):
        return None
    return candidate


def validate_sender(
    *,
    raw_from: str,
    raw_reply_to: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> SenderVerdict:
    """Run all sender checks. Returns one verdict.

    `raw_from` and `raw_reply_to` are the raw header values
    (potentially "Display Name <addr@host>" format).

    `workflow_id` opt-in: when provided, this skill's per-address
    allowlist (under `allowed_from_addresses[workflow_id]` in the
    config) is consulted. Without it, NO per-address bypass applies
    — strict domain check only (fail closed per #6).
    """

    cfg = _config()

    from_addr = _extract_address(raw_from)
    if from_addr is None:
        return SenderVerdict(accepted=False, reason="from_unparseable")

    # Forward detection: From == operator's own address.
    if cfg.own_addresses and from_addr in cfg.own_addresses:
        return SenderVerdict(
            accepted=False, reason="from_is_operator_likely_forward",
            normalized_from=from_addr,
        )

    # Per-skill address allowlist (for test fixtures). If a workflow_id
    # is provided AND the address is in THAT skill's list, skip the
    # domain check. Without workflow_id, no bypass.
    address_allowed = False
    if workflow_id:
        per_skill = cfg.allowed_from_addresses.get(workflow_id, [])
        address_allowed = from_addr in per_skill

    # Domain allowlist (skipped when address allowlist matches).
    if cfg.allowed_from_domains and not address_allowed:
        from_domain = from_addr.split("@", 1)[1]
        if not any(
            from_domain == d or from_domain.endswith("." + d)
            for d in cfg.allowed_from_domains
        ):
            return SenderVerdict(
                accepted=False,
                reason=f"from_domain_not_allowed:{from_domain}",
                normalized_from=from_addr,
            )

    # Reply-To consistency: if present, must agree on domain with From.
    if raw_reply_to:
        reply_addr = _extract_address(raw_reply_to)
        if reply_addr is None:
            return SenderVerdict(
                accepted=False, reason="reply_to_unparseable",
                normalized_from=from_addr,
            )
        from_domain = from_addr.split("@", 1)[1]
        reply_domain = reply_addr.split("@", 1)[1]
        if from_domain != reply_domain:
            return SenderVerdict(
                accepted=False,
                reason=f"reply_to_domain_mismatch:{reply_domain}_vs_{from_domain}",
                normalized_from=from_addr,
            )

    return SenderVerdict(
        accepted=True, reason="ok", normalized_from=from_addr,
    )
