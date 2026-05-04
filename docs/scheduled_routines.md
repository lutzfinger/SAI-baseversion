# Scheduled Routines

This file is generated from `app/control_plane/scheduled_jobs.py`.

SAI keeps timed jobs on explicit local-time schedules, plus one file-watch routine.
Jobs use a mix of native calendar delivery and local slot state so missed work can
catch up once after wake or reload without repeating indefinitely.

## Inbox tagging

- Schedule: Every 10 minutes.
- Purpose: Tag new inbox email with the SAI taxonomy.
- Catch-up: No catch-up gate; launchd wakes it on the fixed interval.

---

## Operator-facing CLIs (paste-safe; not on a cron yet)

These are text-mode dashboards the operator runs ad-hoc. The future
sai-cost / sai-metrics Slack agents (registered in
`config/channel_allowed_discussion.yaml`; design in
`docs/design_cost_dashboard_slack.md`) will reuse the same data
sources but add natural-language query handling.

### sai-cost — internal cost report

Per-workflow + per-LLM-role cost breakdown from SAI's own audit log
+ `sai_eval_agent.jsonl`. Distinct from
`app/workers/daily_cost_report.py` which posts PROVIDER-API totals.

```bash
python -m scripts.sai_cost_report
```

```bash
python -m scripts.sai_cost_report --hours 168 --json
```

### sai-metrics — eval / regression / quality report

Sizes + last-modified for canaries, edge_cases, disagreements,
true-north datasets + open Loop 4 proposals.

```bash
python -m scripts.sai_metrics_report
```

```bash
python -m scripts.sai_metrics_report --include-true-north --json
```

### sai-health — operational pulse

Service status (launchctl), Slack bot state, eval state, audit-24h
summary, error-24h summary.

```bash
python -m scripts.sai_health
```
