# Cleanup plan — remove operator PII from the public base repo

> **Temporary working doc.** Delete this file at the end of the cleanup (it is a
> task plan, not durable documentation — durable rules live in `PRINCIPLES.md`).
>
> **This file deliberately contains no literal private values** (no real emails,
> names, addresses, invoice numbers, or the operator's real overlay-dir name) so
> the plan itself passes the boundary linter and is safe to commit. A working
> session rediscovers the exact literals with the discovery commands below, or
> from the gitignored `boundary_check_private_terms.txt`.
>
> Notation: `~/<overlay-root>/SAI/` stands for the operator's real private
> overlay directory under `$HOME`.

## Why

`README` and `PRINCIPLES.md` §17/§18/§24 already require this repo to be
**operator-agnostic**: mechanisms (validation, parsing, runtime) are public;
**values** (real emails, names, customers, OAuth, paths, financial data) live in
the operator's **private `SAI` overlay** (`~/<overlay-root>/SAI/`). A code review
found the base repo currently violates this in ~40+ files. This plan strips the
base back to placeholders. The real values belong in the private overlay at the
same relative paths (§18: private wins on path conflict).

The repo's own test for the split (§17): *"would you ship this file to a stranger
who has never seen the operator's data?"* If no → it's private.

## Access constraint

- A session mounted on **`SAI-baseversion`** (public) can do the **strip side**:
  replace real values with placeholders, keep tests green. ← most of this plan.
- **Moving** the real values into the private **`SAI`** overlay requires a session
  that has the `SAI` repo mounted. Do that side there, at matching relative paths.

## Discovery commands (run first, regenerate the inventory)

```bash
# All emails that are NOT obvious synthetic placeholders:
grep -rEohI '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' . \
  --include=*.py --include=*.md --include=*.yaml --include=*.yml \
  --include=*.json --include=*.jsonl --include=*.txt \
  | grep -viE 'example\.(com|org|edu|net)|@external\.example|pied-piper\.example|ourcorp\.example|somecompany\.example|vendor-foo\.example|nope\.com|evil\.com|@x\.com|@y\.com|@third\.com|@another\.com|sister\.example' \
  | sort | uniq -c | sort -rn

# Identity-revealing local paths (operator overlay dir + absolute home paths):
grep -rnIE '_Dev/SAI|/Users/[a-z]' . \
  --include=*.py --include=*.md --include=*.yaml --include=*.txt --include=*.sh

# The boundary linter — the real enforcement gate (see Phase 6). It already
# knows the operator-string and home-path patterns:
python scripts/boundary_check.py
```

## Replacement conventions (keep consistent everywhere)

| Real value (category) | Placeholder to use in base |
|---|---|
| operator primary email | `operator@example.com` |
| operator service/alias email | `sai@example.com` |
| third-party personal contacts | `contact@example.com`, `jane@example.com` |
| customer / edu address | `customer@example.edu` |
| operator full name (hardcoded default) | remove default → load from overlay config; placeholder `Operator Example` |
| customer org names | `ACME`, `Globex` |
| vendors | `Taxi`, `Hotel`, `Airline`, `Rideshare` |
| QBO invoice/purchase IDs + `intuit`-host txn links | synthetic ids (`1001`, `2001`), **no real links** |
| home city / street address | `Hometown`, `123 Example St, Anytown` |
| operator overlay dir `~/<overlay-root>/SAI/` | `$SAI_OVERLAY` or `~/your-sai-overlay/` |
| absolute home paths under `$HOME` | `$HOME/` |

---

## Phase 1 — Real emails → synthetic placeholders  (CRITICAL)

Replace per the table above. Where a value is a genuine operator binding (e.g. an
`operator_email` fallback), read it from overlay config instead of a literal.

Files:
- `skills/receipt-collector/lib/invoice_intent.py`
- `skills/receipt-collector/lib/general_assistant.py`
- `skills/receipt-collector/lib/intent_router.py`
- `skills/receipt-collector/lib/dispatch_agent.py`
- `skills/receipt-collector/lib/ad_hoc_decomposed.py`
- `skills/receipt-collector/tests/test_invoice_intent.py`
- `skills/receipt-collector/tests/test_email_runner_reply.py`
- `skills/receipt-collector/tests/test_pre_write_critique.py`
- `skills/receipt-collector/tests/test_qb_invoice_methods.py`
- `tests/test_trip_mileage_log.py`
- `tests/fixtures/trip_mileage/config_ok.yaml`
- `scripts/sai_email_skill_intake.py`
- `skills/receipt-collector/docs/STATUS.md`, `docs/DECISIONS.md`
- `docs/ad-hoc-autoexec-and-tag-2026-05-28.md`

⚠️ `tests/runtime/test_boundary_check.py` — inspect, do **not** blind-replace.
This is the linter's own test; it may use realistic-looking strings *on purpose*
to prove the linter catches them (the scanner already self-exempts this file).
Keep its intent; use clearly-fake-but-matching samples if needed.

## Phase 2 — Hardcoded operator names → de-hardcode  (CRITICAL)

Remove the operator's real name from code defaults; require it via overlay config.
- `app/tools/other_to_personal_router.py` (~line 50): `my_names or [<real names>]` → `my_names or []`
- `app/tools/personal_relationship_routing.py` (~line 905): same change
- `app/tools/contact_investigation.py` (~line 63): drop hardcoded `my_names=[...]` → from config
- Name strings in receipt-collector tests/fixtures → `Operator Example`, `Pat Example`, etc.

## Phase 3 — Live financial data → out of base  (CRITICAL — highest severity)

`skills/receipt-collector/docs/STATUS.md` has a live-trip section with **real**
QBO invoice/purchase IDs, **clickable `intuit`-host txn links**, amounts, vendors,
airline confirmation codes, a real customer, and a named person.

- **Move** that whole live-trip block to the private overlay
  (`~/<overlay-root>/SAI/skills/receipt-collector/docs/STATUS.md`).
- In base, replace with a **synthetic worked example** (Invoice `1001`, customer
  `ACME`, generic vendors, no real links).
- Scrub real customer/vendor/place/confirmation specifics → generic, in:
  - `skills/receipt-collector/edge_cases.jsonl`, `workflow_regression.jsonl`
  - `skills/receipt-collector/prompts/cost_compiler_agent.md`
  - `skills/receipt-collector/cost_compiler_agent.surface.yaml`
  - `skills/receipt-collector/lib/parse_trigger.py`, `lib/slack_runner.py`, `lib/cost_compiler_agent.py`
  - `skills/receipt-collector/tests/test_three_case_dispatch.py`
  - `skills/receipt-collector/docs/PLAN.md`, `docs/DECISIONS.md`
  - `README.md` (real workshop/customer names)
  - verify: `app/agents/sai_operator_dm_agent.py`, `app/skills/skill_run_parser.py`,
    `scripts/sai_dispatch.py`, `scripts/sai_email_skill_intake.py`,
    `eval/sai_operator_dm_agent_canaries.jsonl` (matched on financial keywords)

## Phase 4 — Identity-revealing paths → genericize  (CRITICAL)

`~/<overlay-root>/SAI/` → `$SAI_OVERLAY` / `~/your-sai-overlay/`; absolute home
paths under `$HOME` → `$HOME/`.

Files (from path discovery grep):
- `HANDOFF.md`, `PHASE-2-DONE.md`, `PHASE-3-PARTIAL-DONE.md`
- `app/agents/sai_operator_dm_agent.py`, `app/connectors/google_sheet.py`,
  `app/runtime/overlay.py`, `app/skills/manifest.py`, `app/skills/manifest_validator.py`
- `scripts/sai_dm_agent_subprocess.py`
- `docs/cowork_skill_creator_prompt.md`, `docs/onboarding_wizard_prompt.md`,
  `docs/ad-hoc-autoexec-and-tag-2026-05-28.md`
- `skills/receipt-collector/README.md`, `runner.py`, `docs/STATUS.md`, `docs/workflow.md`,
  `lib/cost_compiler_tools.py`, `lib/email_runner.py`, `lib/invoice_logic_bridge.py`
- `skills/trip-mileage-log/MANIFEST.txt`, `README.md`

⚠️ **Do NOT touch as "leaks"** — these mention the patterns *by design*:
- `PRINCIPLES.md` §24 (documents what the linter catches, incl. home paths)
- `scripts/boundary_check.py`, `boundary_check_allowlist.txt`,
  `boundary_check_private_terms.example.txt`, `tests/runtime/test_boundary_check.py`
  (the linter self-exempts these). Review individually; keep one genericized
  example of the overlay-path *convention* in the README.

## Phase 5 — Real locations → synthetic  (CRITICAL)

Home city, street address, and identifiable place names → generic.
- `tests/fixtures/trip_mileage/config_ok.yaml` (`home_label`)
- `skills/trip-mileage-log/mileage_logic.py`, `trip_config.py`
- `skills/trip-mileage-log/edge_cases.jsonl`, `workflow_regression.jsonl`, `canaries.jsonl`
- `tests/test_trip_mileage_log.py`, `tests/test_calendar_events_on_date.py`

## Phase 6 — Enforce so it can't regress  (do this, then it self-checks)

1. Populate the **gitignored** `boundary_check_private_terms.txt` (template:
   `boundary_check_private_terms.example.txt`) with the real terms: operator name,
   personal email domains, third-party contacts, customer/place names, the overlay
   dir name, the OS username, airline confirmation codes, etc. This file is never
   committed (`.gitignore`) — it's the operator's private match list.
2. Run `python scripts/boundary_check.py` — it must report **clean**. Resolve every
   hit (replace, or add an allowlist entry **with a justifying comment** per §24).
3. Confirm the linter runs in `.pre-commit-config.yaml` **and** GitHub Actions
   (`.github/workflows/`). It already runs as the `boundary-check` CI job — keep it
   green.

## Acceptance

```bash
pytest -q                          # green with synthetic data
python scripts/boundary_check.py   # clean
# zero hits for retired literals (run with private_terms populated):
grep -rnIE '_Dev/SAI|/Users/[a-z]|intuit\.com.*txnId' .
```

## Sequencing for a new session

1. Run the discovery commands; regenerate the file inventory (paths drift).
2. Phases 1→5 are independent; do them in any order. Keep `pytest` green after each.
3. Phase 6 last — populate private_terms, run the linter, fix remaining hits.
4. On the public side: replace only. On the private `SAI` side (separate session):
   add the real values at matching relative paths.
5. Delete this file. Commit. Open/refresh the PR.
