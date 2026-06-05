"""The logic bridge imports the ONE source of truth (invoice_lib) from the
invoice-draft-and-send skill — proving the daemon reuses it rather than
duplicating money logic."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_bridge_imports_real_invoice_lib():
    from lib import invoice_logic_bridge as L
    # default-services proves it's the real parser, not a stub
    p = L.parse_trigger("send gregory an invoice over 1500")
    assert p.name == "gregory"
    assert p.services == "teaching / speaking"
    assert str(p.amount) == "1500"
    # the classes/callables the daemon needs are all present
    for attr in ("candidates_from_threads", "choose_customer", "resolve_customer",
                 "decide_service_item", "line_for_invoice", "classify_reply",
                 "build_summary", "SummaryFacts", "trigger_hash", "PendingStore"):
        assert hasattr(L, attr), attr
