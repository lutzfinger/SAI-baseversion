# trip-mileage-log

Log a **local-drive** trip into the operator's mileage Google Sheet — column
**H** (round-trip miles), **I** (100% business), **J** (reason) — from a plain
sentence like *"yesterday I went to Berkeley"* or *"I am going to Berkeley"*.

Deterministic SAI workflow (no LLM). Read-only until the operator ✅; the actual
sheet write happens only in `send_tool.py`, behind a kill-switch that defaults
OFF (PRINCIPLES §2/§9/§16e).

## What it does
1. **Parse the date** from the sentence (yesterday / today / now / "I am going" /
   explicit date), and whether it's *prospective*.
2. **Read that day's Google Calendar** to find where you drove. Two distinct
   places → one **chained loop** `home → A → B → home`.
3. **Confirm it was a drive, not a flight** — fails closed if the sheet row has
   an airport (cols C/D) or the calendar has a flight event. (Column **G
   "Travel Day" is intentionally NOT used** — it is `TRUE` on local-drive days
   too.) Also fails closed on a morning≠evening **relocation**.
4. **Look up round-trip miles** from the workbook's `Distance MTV to` tab.
   `single = round trip`; `two = rt(A)/2 + leg(A,B) + rt(B)/2` (one-way ≈
   round-trip / 2). On a miss it **asks** you for the number and remembers it.
5. **Guard against overwrite** — refuses to clobber an already-filled H/I/J row
   unless you confirm.
6. **Stage an approval proposal** with the exact H/I/J it will write.

## Run (read-only — stages a proposal, never writes)
```bash
python skills/trip-mileage-log/runner.py \
  --utterance "yesterday I went to Berkeley" --today 2026-06-04
```
First runs ask-and-store distances (the `Distance MTV to` tab starts empty);
reply with the round-trip miles and they're remembered. You can pre-seed common
places via `seed_distances` in the private config.

## Configure (private values — §17/§18)
Operator values live in `~/Lutz_Dev/SAI/config/trip_mileage.yaml` (home label,
sheet URL, tab gids, kill-switch name, place aliases). The base skill is
values-free; the overlay merges this to `~/.sai-runtime/config/trip_mileage.yaml`.

## Enable the write (after a green dry-run)
The sheet write fires from `send_tool.py` on an approved proposal ONLY when the
kill switch is on:
```bash
export SAI_TRIP_MILEAGE_SEND_ENABLED=1
python skills/trip-mileage-log/send_tool.py <approved-proposal.yaml>
```

## Files
See `MANIFEST.txt`. Tests: `tests/test_trip_mileage_log.py`,
`tests/test_calendar_events_on_date.py` (all offline).
