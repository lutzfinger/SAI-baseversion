"""invoice_logic_bridge — import the ONE source of truth for the invoice
pure logic (parse / resolve / summary / state) from the invoice-draft-and-send
skill, instead of duplicating ~400 lines of money logic in this daemon.

Per PRINCIPLES #33a this cross-skill import is a deliberate, documented,
temporary coupling. Follow-up (tracked in LOOSE-ENDS): promote the pure logic
to a shared primitive (app/canonical/invoice_logic.py) and import THAT from
both the claude_code skill and this daemon. Single-source-of-truth is chosen
over duplication specifically because this is money logic (a parse/resolve bug
fixed in one copy must not linger in another).

Path resolution order (fail closed if none import):
  1. $SAI_INVOICE_LIB_DIR              (explicit override, used by tests)
  2. ~/.sai-runtime/skills/invoice-draft-and-send/lib   (production runtime)
  3. ~/SAI/skills/invoice-draft-and-send/lib   (working-repo source)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_CANDIDATES = [
    os.environ.get("SAI_INVOICE_LIB_DIR"),
    os.path.expanduser("~/.sai-runtime/skills/invoice-draft-and-send/lib"),
    os.path.expanduser("~/SAI/skills/invoice-draft-and-send/lib"),
]


def _load():
    last_err = None
    for d in _CANDIDATES:
        if not d or not Path(d, "invoice_lib.py").exists():
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import invoice_lib  # type: ignore
            return invoice_lib
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ImportError(
        "invoice-draft-and-send invoice_lib not found on any known path "
        f"({[c for c in _CANDIDATES if c]}); last error: {last_err}"
    )


invoice_lib = _load()

# Re-export the callables/classes the daemon's invoice flow uses.
parse_trigger = invoice_lib.parse_trigger
normalize_amount = invoice_lib.normalize_amount
candidates_from_threads = invoice_lib.candidates_from_threads
choose_customer = invoice_lib.choose_customer
resolve_customer = invoice_lib.resolve_customer
decide_service_item = invoice_lib.decide_service_item
line_for_invoice = invoice_lib.line_for_invoice
cc_address = invoice_lib.cc_address
classify_reply = invoice_lib.classify_reply
build_summary = invoice_lib.build_summary
SummaryFacts = invoice_lib.SummaryFacts
strip_em_dashes = invoice_lib.strip_em_dashes
trigger_hash = invoice_lib.trigger_hash
PendingStore = invoice_lib.PendingStore
Candidate = invoice_lib.Candidate
Decision = invoice_lib.Decision
InvalidAmount = invoice_lib.InvalidAmount
InvalidTrigger = invoice_lib.InvalidTrigger
