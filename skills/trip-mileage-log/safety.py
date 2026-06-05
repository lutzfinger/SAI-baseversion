"""Different-model safety gate for the AUTONOMOUS mileage write (§7a / §33).

Composition only (§33a): it uses the EXISTING cascade + the EXISTING
`safety_gate_high` role — no new primitive, no LLM SDK imported here. Fail-closed
(#6): any error / abstain / malformed verdict ⇒ ``safe=False`` (the write is
refused). Mirrors `cornell-qualtrics-survey/run_autonomous.safety_reviewer`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class SafetyVerdict:
    safe: bool
    reason: str = ""


def review_mileage_write(draft: dict, *, predict: Optional[Callable[..., Any]] = None) -> SafetyVerdict:
    """Ask a DIFFERENT model 'is this autonomous mileage write safe & wanted?'.

    `predict` is injected in tests; production uses `app.llm.cascade.predict`
    (role `safety_gate_high`, never a model id — §24b).
    """
    try:
        if predict is None:
            from app.llm import cascade  # lazy: keep skill import-light + offline-testable
            predict = cascade.predict
        from app.llm.provider import LLMRequest

        prompt = (
            "You are the safety reviewer for an AUTONOMOUS agent that is about to "
            "write a business-mileage row to the operator's IRS tax sheet. There is "
            "no human approving this write — your verdict is the gate.\n\n"
            f"Date: {draft.get('date_str', '?')}\n"
            f"Route: {draft.get('route', '?')}\n"
            f"Destinations (from calendar): {draft.get('places', [])}\n"
            f"Miles (col H): {draft.get('H')}   Business% (col I): {draft.get('I')}\n"
            f"Reason (col J): {draft.get('J', '')}\n"
            f"Not-a-flight evidence: {draft.get('not_flying_evidence', '')}\n"
            f"Deterministic plausibility check: {draft.get('plausibility', 'ok')}\n\n"
            "Approve (safe=true) ONLY if ALL hold: it is a believable LOCAL DRIVE "
            "(not a flight); the miles are plausible for driving between these "
            "places; the destinations are consistent with the calendar reason; and "
            "business is 100%. Otherwise safe=false with a short reason."
        )
        request = LLMRequest(
            prompt=prompt,
            response_schema={
                "type": "object",
                "properties": {"safe": {"type": "boolean"}, "reason": {"type": "string"}},
                "required": ["safe"],
            },
            response_schema_name="MileageSafetyReview",
            max_output_tokens=200,
        )
        resp = predict("safety_gate_high", request)
        out = getattr(resp, "output", None) or {}
        return SafetyVerdict(out.get("safe") is True, str(out.get("reason", "")))
    except Exception as exc:  # fail-closed on any error / abstain / malformed (#6)
        return SafetyVerdict(False, f"safety review unavailable: {type(exc).__name__}: {exc}")
