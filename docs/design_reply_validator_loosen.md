# Design — loosen `ReplyDraft._tone_appropriate` validator (2026-05-04)

**Status:** approved by operator 2026-05-04, implementing now
**Maps to:** `app/canonical/reply_validation.py`,
`docs/e1_principles_audit.md` decision #2 (path B),
PRINCIPLES.md §33a (framework primitive change → design doc + ship cycle)
**Triggered by:** the e1 dry-run gap analysis (operator wanted
"Sorry to hear that" empathy on no_exception cases; the validator
banned it).

---

## What changes

The `_tone_appropriate` validator currently REJECTS auto-empathy
phrases (`sorry to hear`, `difficult time`, `hope you're ok`) when
the draft's `classification == "no_exception"`. After this change,
empathy phrases are **allowed on any classification**.

**Before:**
```python
@model_validator(mode="after")
def _tone_appropriate(self) -> "ReplyDraft":
    if self.classification == "no_exception":
        for pat in _AUTO_EMPATHY_PATTERNS:
            if pat.search(self.body):
                raise ValueError(
                    f"no_exception classification must not use "
                    f"auto-empathy phrase: {pat.pattern!r}"
                )
    return self
```

**After:**
The `_tone_appropriate` validator + `_AUTO_EMPATHY_PATTERNS` constant
are removed entirely. The model still enforces:
- `must_self_identify_as_ai` (unchanged)
- `must_not_promise_extension` (unchanged)
- `length_in_bounds` (unchanged)
- `cc_must_be_well_formed` (unchanged)
- `no_other_student_names` (unchanged)

---

## Why

Operator's call. Original judgment was "auto-empathy on routine
extension requests is the wrong tone — sounds patronizing." The
revised judgment is "warm + acknowledging is the right tone for
ALL student-facing communication, even routine cases."

The other validators (no AI-impersonation gap, no extension
promises, no PII leak) cover the genuinely-hard rules. Tone is a
matter of preference; the operator picks.

---

## Risks + mitigations

**Risk: empathy used cynically as filler.** A future LLM-drafted
reply might say "sorry to hear" reflexively even when the student
didn't say anything sad. **Mitigation:** the body template stays
operator-controlled (deterministic for now); when an LLM drafter
ships, the second-opinion gate (#16f / #10) catches tone-mismatch
in review.

**Risk: regression for any skill that depended on the validator.**
Today only e1 (`cornell-delay-triage`) instantiates `ReplyDraft`.
Looser validation never CAUSES a previously-valid draft to become
invalid — only the reverse. So no skill breaks.

---

## Test impact

- `tests/canonical/test_reply_validation.py` —
  `test_no_exception_must_not_use_auto_empathy` flips to assert
  the OPPOSITE (no_exception drafts CAN use empathy phrases).
- `test_exception_classification_allows_empathy` becomes a
  general assertion that empathy is allowed on any
  classification.

No e1 runner test breaks: the e1 deterministic template doesn't
currently use empathy phrases on no_exception — it just gets the
freedom to start using them once the operator updates the
template.

---

## Skill-creator prompt update

The "Framework-validator constants you CANNOT soften" table in
`docs/cowork_skill_creator_prompt.md` still applies — the
`must_self_identify_as_ai` + `must_not_promise_extension` +
length validators stay strict. The `_tone_appropriate` row is
REMOVED from that table since the validator no longer exists.

---

## Files touched

- `app/canonical/reply_validation.py` — remove `_tone_appropriate`
  validator + `_AUTO_EMPATHY_PATTERNS` constant
- `tests/canonical/test_reply_validation.py` — flip the empathy
  assertions
- `docs/cowork_skill_creator_prompt.md` — remove row from the
  strict-validator table
