# AD_HOC auto-execute + terse reply + thread-tag fix (2026-05-28)

**Scope:** the live sai@ email daemon (`skills/receipt-collector`,
launchd `com.sai.receipt-collector.email-listen`). This is the agent
that actually replies to operator emails sent to sai@ — NOT the
dormant main-SAI `sai_email_interaction` worker, and NOT the main-SAI
`run_sai_inbox_dispatcher.py`.

**Why this doc:** a session built risk-tiered AD_HOC auto-execution
into the daemon. Most of the change lives inside the operator's
uncommitted `receipt-collector/lib/` WIP, so this doc is the durable
record of what changed + why + how it was verified, independent of
when the lib gets committed.

## What changed (operator-facing behavior)

For sai@ tasks the daemon CAN do with low-risk, reversible steps, it
now **just does them** in one turn and replies tersely — instead of
proposing and waiting for "y".

Two task classes auto-execute:

- **Draft a reply** ("draft a Gmail reply to karin about my latest
  Forbes article"): search Gmail for the person + scan the operator's
  latest Forbes articles + detect the recipient's language + **create
  a Gmail draft** (reversible — never sent). Terse reply, tagged
  `SAI/plan`.
- **Calendar block** ("book travel time to dinner from MtView
  tomorrow"): read the calendar for the target event + LLM-estimate
  the route minutes + **create a calendar event** (reversible).
  Terse reply, tagged `SAI/plan`.

Terse reply shape (operator's exact spec):
```
Auto Execution, since low risk
- Found recipient: karin.finger@gmx.net
- Found latest Forbes Article: The Missing Moat In AI: Your Eval Data
- Checked main language in current emails: German
- Drafted Email: "Liebe Karin, …  <url>"
- Email is in Drafts. Ready for you to send.
```

## Risk model (the principle)

- **Read-only** (Gmail search, Forbes scan, calendar read) → always
  auto-run.
- **Reversible write** (Gmail DRAFT, calendar EVENT) → auto-run, then
  report. The operator reviews/edits/deletes; nothing is irreversibly
  committed. These are NOT gated behind y/n.
- **Irreversible** (SEND an email, pay, post publicly, delete, edit
  code/prompts/policy) → NOT auto-run. Tagged `SAI/proposal`, honest
  one-line "I can't do this" (no Claude-Code-prompt spam).

## Thread status labels (operator's mapping)

The daemon now applies a status label after handling + strips the
legacy `SAI/Input`:
- `SAI/done`     — SAI answered it (no further action).
- `SAI/plan`     — SAI planned **and acted** (draft / calendar block
                   created). The Karin + travel-block outcome.
- `SAI/proposal` — SAI does NOT know / cannot do it right now.

(Root-cause note: `SAI/Input` was being auto-applied by a **Gmail
filter**, not SAI code. The operator removed that filter on
2026-05-28, so the daemon's positive label now sticks. The daemon's
`SAI/Input` removal is defensive cleanup for legacy threads.)

## Feedback / steering (one turn, no re-ask loop)

After the draft/event is auto-created, the operator can steer:
- `y` / `sg` / `k` / `yes` → "already done, it's in Drafts/Calendar".
- `no, use karinbdohm@web.de` → **re-runs** with the corrected
  recipient (steering is detected by an email address in the reply,
  checked BEFORE the reject path so it re-targets instead of dropping
  — per #16g pending-intents-never-drop).
- `n` (bare) → drop; tells the operator to delete the artifact.

## Files

NEW (committed with this doc — 100% this session's work):
- `skills/receipt-collector/lib/ad_hoc_decomposed.py`
  Read-only tool adapters (gmail_search dedup, forbes_latest file
  scan), calendar read/write/route-estimate, the `auto_execute_ad_hoc`
  orchestrator (router LLM → reads → reversible write → terse reply),
  terse formatters, draft + calendar builders.

EDITED (these remain in the operator's UNCOMMITTED `lib/` WIP — NOT
committed by the session to avoid bundling unrelated WIP; the operator
commits the lib as a unit):
- `email_runner.py` — AD_HOC branch now calls `auto_execute_ad_hoc`
  on turn 1 + `_apply_status_label`; `_route_ad_hoc_reply` rewritten
  for the auto-exec feedback model; added `_ensure_label_id` +
  `_apply_status_label`.
- `dispatch_agent.py` — Decision Rule #1 rewritten: reversible writes
  (Gmail draft, calendar event) are NOT irreversible → such tasks are
  AD_HOC_CAPABLE, not WORKFLOW_SUGGESTION.
- `general_assistant.py` — `propose_ad_hoc_steps` tries the decomposed
  path first (fallback to the old propose-only flow).
- `approval.py` — APPROVE_TOKENS gained `k`, `kk`, `yep`, `yup`,
  `sounds good`.

## Calendar capability (correction)

Calendar WRITE needs no new OAuth. The token at
`~/Library/Application Support/SAI/tokens/meeting_calendar_token.json`
already carries `calendar.events`. The daemon's default
`~/.SAI/calendar_token.json` is readonly; `ad_hoc_decomposed` points
calendar ops at the meeting token (it has both read + write scopes).
Route timing is an LLM estimate (no maps API) — rough by design,
labeled as such.

## Live verification (real LLM + real Gmail/Calendar)

- Karin draft → real Gmail draft to `karin.finger@gmx.net`, German
  body (auto-detected), terse reply, `SAI/plan`. ✓
- Travel block → real calendar event "Travel: MtView → Kaiyo
  Restaurant", terse reply, `SAI/plan`. ✓
- `dispatch_agent.classify` on both task types → `AD_HOC_CAPABLE`
  (high). ✓
- Feedback re-target ("no, use karinbdohm@web.de") → real draft to the
  corrected address. ✓

## Known caveats / follow-ups

- Route minutes are a rough LLM estimate; a maps API is a precision
  upgrade.
- Gmail recall for the recipient was widened (`(name) -from:operator`,
  max_results 15, single-clear-match-proceeds). Edge cases with
  multiple same-name contacts will still ask.
- Formal pytest files were NOT written this session — behavior was
  verified via live end-to-end runs + offline mocked checks. Adding
  unit tests for `ad_hoc_decomposed` is a clean follow-up.

## Operator: how to commit the lib (the WIP + the session's edits)

The session committed only `ad_hoc_decomposed.py` + this doc (clean,
standalone). The 4 edited files above are part of your uncommitted
`receipt-collector/lib/` WIP. When ready, commit the lib as a unit:

```
cd ~/Lutz_Dev/SAI-baseversion
git add skills/receipt-collector/lib/
git commit -m "receipt-collector: AD_HOC auto-execute + terse reply + status-label tagging"
```

(That will pick up `ad_hoc_decomposed.py` as already-tracked + add the
rest of the lib + your other WIP in it.)
