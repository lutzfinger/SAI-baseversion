# trip-mileage-log — security & trust boundaries (v0.3.0, autonomous daemon)

This is a **headless autonomous** skill that writes to a **tax** sheet from an
**email** trigger with **no per-run human approval**. The safety envelope is the
whole point — every layer below is load-bearing.

## Who may trigger (fail-closed)
- A trigger thread is acted on ONLY when the request email is from an
  **allowlisted operator address** (`config.operator_addresses`); any other
  sender is ignored (fired once, no reply, no action).
- The daemon replies ONLY to `config.reply_to` (the operator), From `config.sai_from`.

## Pre-approved + mandatory safety gate (PRINCIPLES §7a / §33 item 4)
- The sheet write is `pre_approved: true` (the operator's one-time sign-off), so
  there is no per-run click. The manifest therefore carries a `second_opinion`
  tier (`safety_gate_high`), and the runtime runs that **different-model** gate
  before every write — fail-closed on anything but a clear "safe" verdict (#6).
  (The producer is deterministic, so an Anthropic reviewer is still a distinct
  surface — writer ≠ reviewer, §21.)

## Deterministic guards (do NOT depend on the LLM)
Fail-closed (no write; a "needs human" reply) on:
- non-operator sender · unparseable date · no matching date row;
- a **flight day** (airport in C/D, or a flight calendar event) — column G is a
  generic travel flag and is intentionally NOT used as a flight signal;
- a **relocation** (morning ≠ evening location);
- more than two destinations;
- an **unresolvable** distance (geocode/route failure or no connector) — it never
  asks or guesses;
- an **implausible** loop (> `max_local_miles`, default 300 — a flight, not a drive);
- an already-filled H/I/J row without confirmed overwrite.

## The write path
- Only `send_tool.apply_approved_proposal` mutates the sheet (H/I/J + appends new
  round-trips to the `Distance MTV to` tab). It sits behind the kill-switch
  `SAI_TRIP_MILEAGE_SEND_ENABLED` (defaults **OFF**, §16e), re-validates
  fail-closed (workflow_id, row, miles>0, business 0..100), and re-checks
  overwrite. The `send_enabled` param is a test-only override of the env.

## Idempotency / caps
- `SAI/trip_mileage_attempted` before the write + `SAI/trip_mileage_processed`
  after → fires once. Per-day write cap (`max_writes_per_day`). A fail-closed
  path still marks processed (no re-loop). **Known window:** a crash between the
  attempted and processed markers could re-run on the next poll; the overwrite
  guard limits the blast radius (an identical re-write).

## Distance source
- Keyless OSRM (route) + Nominatim (geocode) via the `distance` framework
  connector; fail-closed on HTTP/timeout/429/parse; results cached to the sheet
  (each place hits the network once). **Partial-write note:** if the row writes
  but the distance-tab append fails, the tab is simply un-cached and re-resolves
  next time (harmless).

## LLM / models
- The only LLM is the safety gate, addressed by **role** `safety_gate_high`
  (never a model id, §24b). No vendor SDK is imported in skill code.
