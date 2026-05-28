"""
LLM-call cost log + daily budget cap for the receipt-collector.

Every model call writes one line to ~/Library/Logs/SAI/llm_costs.jsonl so
the operator can audit how much each receipt cost to extract, and the
skill can enforce a daily-budget cap.

Cost policy (per the operator):
  * Prefer deterministic rules (no LLM).
  * If an LLM is needed, prefer the cheapest capable tier (Haiku for vision).
  * Only escalate to Sonnet when the cheaper tier returns low confidence.

Daily budget cap:
  * Per SAI principle #28 (hard ceilings, not queues). When today's spend
    plus an upcoming call would exceed the cap, raise BudgetExceeded.
    Callers must catch and fail closed (per #6).
  * The cap is read from the overlay (`policy.daily_llm_cap_usd`) and
    is OPERATOR-SUPPLIED. The base skill carries no number.
  * Disabled (no enforcement) when no cap is configured — the log
    still writes so spend stays visible.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_LOG_PATH = Path(os.path.expanduser("~/Library/Logs/SAI/llm_costs.jsonl"))


class BudgetExceeded(Exception):
    """Raised when an LLM call would push today's spend past the daily cap.

    Attributes:
        cap_usd: configured daily cap
        today_usd: today's spend BEFORE this call
        upcoming_usd: the call's estimated cost
        skill: skill name (for logging)
        step: step name (for logging)
    """

    def __init__(self, cap_usd: float, today_usd: float, upcoming_usd: float,
                 skill: str, step: str):
        self.cap_usd = cap_usd
        self.today_usd = today_usd
        self.upcoming_usd = upcoming_usd
        self.skill = skill
        self.step = step
        super().__init__(
            f"LLM daily cap exceeded: today=${today_usd:.4f} + "
            f"upcoming=${upcoming_usd:.4f} > cap=${cap_usd:.2f} "
            f"(skill={skill!r}, step={step!r}). Operator must raise "
            f"`policy.daily_llm_cap_usd` in overlay or wait for tomorrow."
        )


def log_call(
    *,
    skill: str,
    step: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    usd_cost: float,
    note: str = "",
) -> None:
    """Append one JSONL row for an LLM call."""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "skill": skill,
        "step": step,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd_cost": round(usd_cost, 6),
        "note": note,
    }
    with _LOG_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def today_usd_total(skill: str | None = None) -> float:
    """Sum USD cost for calls logged today (optionally filter by skill)."""
    if not _LOG_PATH.exists():
        return 0.0
    today = time.strftime("%Y-%m-%d")
    total = 0.0
    with _LOG_PATH.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not row.get("ts", "").startswith(today):
                continue
            if skill and row.get("skill") != skill:
                continue
            total += row.get("usd_cost", 0.0)
    return round(total, 4)


def enforce_daily_cap(
    *,
    skill: str,
    step: str,
    upcoming_usd_cost: float,
    overlay: dict | None = None,
    cap_usd: float | None = None,
) -> None:
    """Raise `BudgetExceeded` if making this call would push today past the cap.

    Resolution order:
      1. Explicit `cap_usd` argument (for tests).
      2. `overlay['policy']['daily_llm_cap_usd']` from operator overlay.
      3. No cap configured → return without raising (cost log still
         records spend; the operator opted out of enforcement).
    """
    resolved_cap: float | None = None
    if cap_usd is not None:
        resolved_cap = float(cap_usd)
    elif overlay:
        pol = overlay.get("policy") or {}
        v = pol.get("daily_llm_cap_usd")
        if v is not None:
            resolved_cap = float(v)

    if resolved_cap is None:
        # No cap configured. Per #1 + #6, refuse silent enforcement —
        # the operator has explicitly opted out OR not yet configured.
        # Caller can detect by checking today_usd_total() themselves.
        return

    today = today_usd_total(skill)
    if today + float(upcoming_usd_cost) > resolved_cap:
        raise BudgetExceeded(
            cap_usd=resolved_cap,
            today_usd=today,
            upcoming_usd=float(upcoming_usd_cost),
            skill=skill,
            step=step,
        )
