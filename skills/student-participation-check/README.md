# student-participation-check

Composed SAI workflow. Count how often a course operator called out each student by first name during recorded course sessions in Granola, and write the counts into a Google Sheet.

## Pipeline

```
operator input → granola-fetch (atomic, agent-orchestrated)
              → fuzzy-name-count (atomic, library import)
              → google_sheet append columns (library import)
              → crosscheck (rules tier)
              → persistent logfile append
```

No LLM. No approval gate. Pure deterministic data transform.

## Inputs (passed at invocation)

| Flag | Required | Meaning |
|---|---|---|
| `--transcripts <dir>` | yes | Directory of SAI-standard `<meeting_id>.json` files (produced by `granola-fetch`). |
| `--sheet <url>` | yes (unless `--dry-run`) | Google Sheet URL. Roster read from column A by default. |
| `--students-file <path>` | alt. to `--sheet` | Local text file, one full name per line. |
| `--name-column <letter>` | no | Column letter for the name (default A). |
| `--worksheet <name>` | no | Tab name (default: first tab). |
| `--threshold <int>` | no | Base fuzzy threshold (default 85). |
| `--any-speaker` | no | Count any speaker, not just the most-talked. |
| `--folder <name>` | no | For the logfile only. |
| `--date-range <range>` | no | For the logfile only. |
| `--dry-run` | no | Skip sheet write; CSV output via `--output-csv`. |
| `--output-csv <path>` | no | Mirror the count matrix to local CSV. |
| `--logfile <path>` | no | Persistent log (default `~/Claude-Logs/SAI/student-participation-check/log.csv`). |

## Outputs

1. **Google Sheet** — N new columns headed `session - YYYY-MM-DD` (chronological), plus a `Total Callouts` column. Appended to the right of any existing columns.
2. **Logfile** (CSV, append-only) — one row per session per run. Used for crosscheck across runs.
3. **Optional local CSV** — same matrix as the sheet write.

## Crosscheck

The skill verifies that `new_session_columns == transcripts_loaded`. If not, it prints an `ERROR:` line and exits non-zero. This catches the case where some transcripts failed to load (null/empty in Granola) and the operator might miss that the session count is off.

## Auth precondition

Reads Google OAuth credentials from `~/.SAI/credentials.json` (one-time setup; reused by every SAI workflow that touches Google APIs).

## Invocation

After the agent has fetched transcripts (`granola-fetch`) into `/tmp/granola-out/`:

```bash
python3 -m skills.student-participation-check.runner \
    --transcripts /tmp/granola-out/ \
    --sheet "<google sheet url>" \
    --folder "<granola folder name>" \
    --date-range "<resolved YYYY-MM-DD..YYYY-MM-DD>"
```
