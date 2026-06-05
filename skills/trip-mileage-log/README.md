# trip-mileage-log (v0.3.0 — headless email daemon)

Log a **local-drive** trip into the operator's mileage Google Sheet — column
**H** (round-trip miles), **I** (100% business), **J** (reason) — triggered by an
**email**, fully headless, with **no per-run sign-off**.

You email `sai@` (or a thread gets tagged `SAI/trip_mileage`) "yesterday I went
to Berkeley"; a launchd poller does the rest.

## Pipeline (per trigger email)
1. **Validate sender** — act ONLY on an allowlisted operator address (fail-closed).
2. **Parse the date** (yesterday / today / "I am going" / explicit).
3. **Read the calendar** for that day → destination(s); two distinct places = one
   chained loop `home → A → B → home`.
4. **Confirm a drive, not a flight** — fail closed on an airport (cols C/D) or a
   flight calendar event (column **G is NOT used** — TRUE on drive days too), or a
   morning≠evening relocation.
5. **Auto-resolve the distance** via the `distance` framework connector (keyless
   OSRM route + Nominatim geocode), **cached** into the `Distance MTV to` tab so
   each place hits the network once. **It never asks you.** Unresolvable → fail closed.
6. **Plausibility bound** — refuse a loop longer than `max_local_miles` (a flight).
7. **Different-model safety gate** (`safety_gate_high`) — "is this safe & wanted?";
   fail closed on anything but a clear yes.
8. **Write** H/I/J (behind the kill-switch) and **reply** on the thread. Any
   fail-closed step → a "needs human" threaded reply, nothing written.

No human approves each write — that's the point. The safety gate + the
deterministic gates + the plausibility bound + the kill-switch are the guard.

## Try it without the daemon (dry-run, no write)
```bash
python skills/trip-mileage-log/runner.py \
  --utterance "yesterday I went to Berkeley" --today 2026-06-04
```
(omit `--write`; the kill-switch also stays off unless `SAI_TRIP_MILEAGE_SEND_ENABLED=1`.)

## Install the daemon (operator)
1. Configure private values in `~/Lutz_Dev/SAI/config/trip_mileage.yaml`
   (`operator_addresses`, `reply_to`, `sai_from`, `distance`, `max_local_miles`,
   `max_writes_per_day`).
2. Wire `run_daemon.main()` to your Gmail operator token + threaded reply sender
   (From `sai@`, To the operator only).
3. Register a `ScheduledJobSpec` (with `wants_watchdog=True`) in
   `SAI/app/control_plane/scheduled_jobs.py` and install via `make ensure-scheduled-jobs`.
4. Flip the kill-switch on after a green dry-run: `export SAI_TRIP_MILEAGE_SEND_ENABLED=1`.

## Composes (does not contain)
The `distance` connector is a separate **framework primitive**
(`app/connectors/distance.py`, §33a) — shipped on its own; this skill composes it.

See `MANIFEST.txt`, `SECURITY.md`. Tests: `tests/test_trip_mileage_log.py`,
`tests/test_distance_connector.py`, `tests/test_calendar_events_on_date.py` (all offline).
