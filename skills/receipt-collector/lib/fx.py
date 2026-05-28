"""
Currency conversion (atomic, base skill).

Trivial helper today: a configurable spot rate. Future extension point
for live ECB / openexchangerates lookups by date.

Public API (atomic):
    convert(amount, from_ccy, to_ccy, rate_table)
    apply_to_line(line, target_ccy, rate_table)
"""
from __future__ import annotations


def convert(amount: float, from_ccy: str, to_ccy: str, rate_table: dict[tuple[str, str], float]) -> float:
    """Convert amount from from_ccy to to_ccy using rate_table.

    rate_table maps (from, to) tuples to multipliers, e.g. {("USD","EUR"): 0.92}.
    Same-currency is a no-op.
    """
    if from_ccy == to_ccy:
        return amount
    key = (from_ccy, to_ccy)
    if key not in rate_table:
        raise KeyError(f"No FX rate configured for {from_ccy}->{to_ccy}. Add to rate_table.")
    return round(amount * rate_table[key], 2)


def apply_to_line(line: dict, target_ccy: str, rate_table: dict[tuple[str, str], float]) -> dict:
    """Return a new invoice line spec with rate converted to target_ccy."""
    src = line.get("source_currency", target_ccy)
    if src == target_ccy:
        return line
    new_rate = convert(line["rate"], src, target_ccy, rate_table)
    rate = rate_table.get((src, target_ccy))
    note = f" Original {src} {line['rate']} @ {rate} = {target_ccy} {new_rate}."
    return {
        **line,
        "rate": new_rate,
        "description": (line.get("description") or "") + note,
    }
