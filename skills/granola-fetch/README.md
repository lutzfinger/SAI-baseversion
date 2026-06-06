# granola-fetch

Atomic SAI skill. Fetch Granola meeting transcripts from a named folder for a date or date range, and save each transcript as a normalized JSON file in an output directory.

## What it does

Given `(folder_name, date_range, output_dir)`, this skill orchestrates the Granola personal-API MCP server (via the agent harness) to:

1. List folders → fuzzy-match the requested folder name → pick a unique folder ID.
2. List meetings in that folder within the date range. Sort chronologically.
3. For each meeting, call `get_meeting_transcript`. Granola transcripts can exceed the agent's inline-context limit; the MCP server auto-saves oversized responses to disk.
4. Read each saved MCP tool-result file, strip the envelope, and write a SAI-standard `<meeting_id>.json` to `output_dir`.

The output JSON shape every downstream SAI skill can rely on:

```json
{
  "meeting_id": "<uuid>",
  "title":      "<meeting title from Granola>",
  "start_time": "<ISO 8601 UTC>",
  "transcript": "<original Granola string OR list of {speaker,text}>"
}
```

## Operator invocation

This skill is **agent-orchestrated** — the agent reads `skill.yaml`, issues the MCP calls in the conversation, then runs the normalizer:

```bash
python3 -m skills.granola-fetch.runner normalize \
    --tool-results ~/.claude/projects/<project>/tool-results/ \
    --date-map /tmp/date_map.json \
    --output-dir /tmp/granola-out/
```

`date_map.json` is `{meeting_id: ISO8601_start_time}` captured from the prior `list_meetings` call (the per-message `get_meeting_transcript` MCP response doesn't include start times — that's why the orchestrator passes them in).

## Composed workflows that use this skill

- `student-participation-check` — Granola → fuzzy-name-count → Google Sheet
- (future: course summarization, per-student feedback)

## Why no API key in this skill?

The Granola personal API key lives in the MCP server's own keystore. This skill never touches the key — it only invokes the MCP tool functions. That keeps the credential surface narrow and consistent with PRINCIPLES.md #5 (per-workflow narrow scopes).
