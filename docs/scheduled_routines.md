# Scheduled Routines

This file is generated from `app/control_plane/scheduled_jobs.py`.

SAI keeps timed jobs on explicit local-time schedules, plus one file-watch routine.
Jobs use a mix of native calendar delivery and local slot state so missed work can
catch up once after wake or reload without repeating indefinitely.

## Inbox tagging

- Schedule: Every 10 minutes.
- Purpose: Tag new inbox email with the SAI taxonomy.
- Catch-up: No catch-up gate; launchd wakes it on the fixed interval.
