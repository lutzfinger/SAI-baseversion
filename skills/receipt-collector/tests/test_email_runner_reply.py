"""_is_operator_sender: an operator reply is recognized whether it comes from
the operator's primary address OR their sai@ alias (the daemon's own messages
are excluded by the X-SAI-Bot header at the call site, not by address)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.email_runner import _is_operator_sender  # noqa: E402

OP = "hello@lutzfinger.com"
SAI = "sai@lutzfinger.com"


def test_primary_counts():
    assert _is_operator_sender("Lutz Finger <hello@lutzfinger.com>", OP, SAI)


def test_sai_alias_counts():
    # the bug: a reply the operator's client sent from sai@ used to be dropped
    assert _is_operator_sender("<sai@lutzfinger.com>", OP, SAI)


def test_third_party_rejected():
    assert not _is_operator_sender("Someone Else <evil@example.com>", OP, SAI)


def test_empty_rejected():
    assert not _is_operator_sender("", OP, SAI)


def test_no_alias_configured_still_matches_primary():
    assert _is_operator_sender("<hello@lutzfinger.com>", OP, "")
    assert not _is_operator_sender("<sai@lutzfinger.com>", OP, "")
