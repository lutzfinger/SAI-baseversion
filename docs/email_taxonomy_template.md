# Email taxonomy template (onboarding)

This is a **template** for the operator's private email taxonomy doc. The
real taxonomy (with the operator's actual L1 buckets, sender lists,
sailing-circle exceptions, etc.) lives in the private overlay at
`docs/email_taxonomy.md`. This file shows the SHAPE that a working
taxonomy doc takes — copy it, adapt the buckets to the operator's life,
and store the personalised version in private.

The two-layer taxonomy (L1 = relationship/domain, L2 = intent) is the
contract that the email-classification cascade enforces: rules tier
applies sender-based shortcuts, the LLM tiers reason about content
when rules abstain. Without a sharp taxonomy doc, both tiers drift —
the LLM hallucinates buckets that aren't in the schema, and the
operator's bucket meanings diverge from what the prompts encode.

---

## Why this doc exists

1. **Single source of truth for what each bucket *means*.** When the
   operator and the LLM disagree on whether a hotel confirmation is
   `personal` (because Lutz booked it for himself) or `updates` (because
   it's a confirmation), the doc settles it. Without the doc, every
   re-tuning session re-litigates the same buckets.

2. **The reference for keyword-rule additions.** Every entry in
   `prompts/email/keyword-classify.md` should be derivable from the doc.
   "Why is `noreply-payments@booking.com` in `invoices` and
   `noreply@booking.com` in `updates`?" — the answer is in the doc's
   bucket definitions.

3. **The grounding for prompt few-shots.** The LLM tier prompts
   (`prompts/email/llm-classify-cloud.md`,
   `prompts/email/llm-classify-gptoss.md`) reference the taxonomy. When
   you add few-shot examples, they must respect the bucket definitions
   here, not invent new ones.

4. **Onboarding for a new instance.** When someone else picks up SAI,
   the taxonomy doc is the first thing they need to understand the
   operator's labelling logic.

---

## Suggested structure

Copy the headings below into your private `docs/email_taxonomy.md` and
fill in the operator-specific content.

### Core Rules

State the universal classification principles. Examples:

- Layer 1 = relationship / domain bucket. Sticky per thread.
- Layer 2 = intent. Per-message.
- **What's the test for `<bucket>`?** Phrase it as a question the LLM
  can answer from the email text alone. Examples:
  - For "discussions about money with humans": "Is a human typing this
    about money?"
  - For "confirmations of any kind": "Is this confirming or notifying
    about state?"
  - For "bills and invoices": "Is this the document of a transaction?"
- Drift guardrails — bucket boundaries that get confused most often.
  Examples: `personal` vs `updates` (booking confirmations), `invoices`
  vs `updates` (statements vs receipts), `friends` vs `customers`
  (sailing-circle vs board work).

### Domain-specific ambiguity

If the operator works at one or more institutions whose email domains
send legitimately mixed mail (work + personal + system), document it.
Use `level1_sender_domain_matches_require_direct_address` for these so
domain matches require direct addressing of the operator in the opening
lines; everything else falls through to the LLM.

### Self-domain rules

If the operator owns a domain that receives inbound inquiries (their
own contact form, etc.), document the self-domain rule. The default
should usually be **don't auto-label first emails** — keep them in
inbox for the human to triage.

### Layer 1: bucket definitions

For each L1 bucket:

- **Test:** the question that decides "is this in this bucket?"
- **Includes:** specific senders / domains / signals (operator-specific)
- **NOT:** the boundaries with adjacent buckets (where confusion happens)
- **Mnemonic:** a short rule of thumb the LLM can apply (optional)

### Layer 2: intent definitions

Same structure for L2 — but L2 is usually more universal across operators
(action_required, meeting_request, casual, etc.) so less personalisation
is needed.

### Special classification rules

Edge cases that don't fit the bucket-by-bucket structure:

- Family relationships overriding automation
- Cold outbound from generic inboxes (`hello@`, `info@`, `contact@`)
  going to `updates` not `personal`
- Sailing-circle (or any peer-leisure circle) staying as `friends` even
  when the words sound transactional
- LinkedIn editorial digests going to `newsletters`, LinkedIn InMails
  going to `updates`

### Output mapping

The Pydantic `Literal` type in `app/workers/email_models.py` plus the
display-name mapping that produces the Gmail labels. The operator
overlay should lock these to whatever `Level1Classification` and
`LEVEL1_DISPLAY_NAMES` enumerate.

---

## Keeping the doc, code, and prompts in sync

Three things that MUST stay aligned:

1. **The Pydantic Literal** in `app/workers/email_models.py`
   (`Level1Classification = Literal[...]`) — the canonical bucket names.
   Adding a bucket to the doc without adding it here means the LLM
   tiers can't even emit the label.

2. **The keyword baseline frontmatter** in
   `prompts/email/keyword-classify.md` — the deterministic rules that
   resolve at the `rules` tier. Every entry should be justifiable by
   pointing at the doc.

3. **The LLM prompts** — `prompts/email/llm-classify-cloud.md` and
   `prompts/email/llm-classify-gptoss.md`. The bucket list inside the
   prompt must match the Literal exactly. The few-shot examples should
   follow the doc's bucket tests.

When you update one, sweep the other two. The boundary linter doesn't
catch this drift; only the regression test does.

---

## Anti-patterns

Things to avoid when designing the taxonomy:

- **Buckets defined by what they USED to be** — bucket meanings drift.
  When the LLM and the doc disagree, fix the doc OR adjust the prompts;
  don't let the gap silently widen.
- **Buckets that overlap by 50%+** — if `personal` and `friends` are
  basically the same except for one sender, merge them or sharpen the
  boundary. Ambiguous buckets are worse than no bucket.
- **Buckets that are really intents** — `meeting_request` is L2, not L1.
  L1 is "who wrote it / what relationship", L2 is "what do they want".
- **Inventing a bucket because the LLM keeps producing it** — if the LLM
  emits `admin` instead of `finance`, that's a prompt problem, not
  a missing-bucket problem. The Literal is the schema; teach the prompt.

---

## How to use this template

1. Copy this file to private as `docs/email_taxonomy.md`
2. Fill in the operator-specific sections (Layer 1 buckets with their
   real senders, special rules, etc.)
3. Make sure `Level1Classification` in `app/workers/email_models.py`
   matches the bucket names you used
4. Make sure `prompts/email/keyword-classify.md` only has rules that are
   justifiable from the doc
5. Run `scripts/quality_check_email_classifier.py --gmail-limit 10` to
   measure rules baseline P/R against your live inbox
6. Iterate: add a rule, re-run, check no regression
