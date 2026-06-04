# trip-mileage-log — security & trust boundaries

## Side effects
- **Only** `send_tool.py` mutates the Google Sheet (writes H/I/J on the date
  row; appends new round-trip distances to the `Distance MTV to` tab).
- The runner/cascade is **read-only**: it reads Calendar + the Sheet and stages
  a YAML proposal. It never writes to the Sheet.

## Gating (PRINCIPLES §2, §9, §16e)
- The sheet write is declared `external_write` + `requires_approval: true`, and
  the cascade ends in a `human` tier that stages a proposal for operator ✅.
- `send_tool.py` sits behind a kill-switch env var
  (`SAI_TRIP_MILEAGE_SEND_ENABLED`) that **defaults OFF**. With it off, the tool
  is a no-op that reports what it *would* have written.

## Fail-closed behavior (PRINCIPLES §6)
The skill refuses (escalates / asks; never guesses) on:
- unparseable date, or no matching date row in the sheet;
- a **flight day** (airport in cols C/D, or a flight calendar event) — this
  column is local *driving* miles only;
- a **relocation** (morning location ≠ evening location → not a clean home
  round trip);
- more than two destinations in a day;
- an **unknown distance** or **inter-stop leg** → asks the operator, then stores
  the answer for reuse;
- an **already-filled** H/I/J row → refuses to overwrite unless the operator
  confirms (`confirm_overwrite`), re-checked again in `send_tool.py`.

## Re-validation in the write path (defense in depth)
`apply_approved_proposal` re-checks the proposal before writing: correct
`workflow_id`, a real row ≥ 2, `miles > 0`, `0 ≤ business ≤ 100`, a non-empty
reason, and refuses an unconfirmed overwrite of populated cells.

## Data & scopes
- Reads: Google Calendar (day events), the mileage Google Sheet. Writes: the
  same sheet (H/I/J + distance tab), only via `send_tool.py`.
- Operator-specific values (home, sheet URL, gids) live in the **private**
  overlay config, never in this base skill (§17/§18).
- No LLM is used; nothing is sent to a model provider.
