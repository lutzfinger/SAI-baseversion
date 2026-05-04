# Design — cost dashboard + P/R chart, delivered via Slack

**Status:** proposal for operator review
**Audience:** operator + future Claude session that builds it
**Maps to:** MVP-GAPS.md Gap 11; PRINCIPLES.md §31 (observability is
built-in, not bolt-on); operator direction 2026-05-02 ("look into
old SAI setup. Use slack and the slack channel as design").

---

## What it is

Daily + weekly + on-demand cost and quality reports, posted to a
dedicated Slack channel. NO web UI. NO separate dashboard service.
Reuses the operator's existing pattern of channel-per-concern
(`sai-status`, `sai-feedback`, `sai-denied`, `sai-errors`,
`tracing-feedback`).

The operator already runs:
- `scripts/run_daily_cost_report_scheduled.sh` (cron 7am)
- `scripts/run_daily_token_usage_report_scheduled.sh` (cron 7am)

These are the BONES. The dashboard is the post-it expansion: take
the data those scripts already gather, post structured Slack blocks
to a `#sai-metrics` channel.

---

## Channel structure (matches existing pattern)

| Channel | Posts | Frequency |
|---|---|---|
| `#sai-status` (existing) | green/red operational state | per cron |
| `#sai-feedback` (existing) | operator-facing requests | per ask |
| `#sai-denied` (existing) | denied actions for audit | per gate |
| `#sai-errors` (existing) | exceptions/cascade errors | per error |
| `#sai-tracing-feedback` (existing) | LangSmith trace links | per anomaly |
| **`#sai-metrics` (NEW)** | cost + P/R reports | daily + weekly |

The new channel keeps cost/quality posts out of the existing
operational channels (so `#sai-status` stays a pure pulse signal).

---

## Daily metrics post (7am, cron-fired)

Single Slack message, structured blocks. Example:

```
:bar_chart: SAI metrics — 2026-05-02

*Workflow costs (last 24h)*
  email-triage-gmail-tagging   $0.41   (32 invocations, $0.013/run)
  sai-eval-agent               $0.04   ( 8 messages,    $0.005/run)
  meeting-followup-draft       $0.12   ( 3 invocations, $0.040/run)
  ─────────────────────────────────
  TOTAL                        $0.57   (down 11% vs 7d avg of $0.64)

*Cascade tier resolution (last 24h, email-triage)*
  rules                        78%   (was 76% 7d avg)
  local_llm                    14%   (was 16%)
  cloud_llm                     6%   (was 7%)
  human                         2%   (was 1%)

*Quality (last 7d, email-triage)*
  L1 accuracy                 91.2%   (target 90%)
  Disagreement queue depth      14   (cap 50; healthy)
  Edge cases dataset            19   (cap 50; healthy)

*Things that need your attention*
  • 2 rule edits awaiting ✅ in #sai-eval (>4h old)
  • Loop 2 batch ready (15 disagreements clustered into 6 themes)
  • [No errors in last 24h] :white_check_mark:
```

---

## Weekly metrics post (Monday 7am)

Same shape as daily but covering 7 days, with delta vs prior 7 days.
Adds:

- Cost trend mini-chart (ASCII or unicode block characters):
  ```
  Cost over 14d:
  Mon ▁  $0.42
  Tue ▂  $0.51
  Wed ▃  $0.63
  Thu ▂  $0.49
  Fri ▁  $0.44
  Sat ▁  $0.38
  Sun ▁  $0.41
  ```
- P/R trend per workflow
- Top-3 highest-cost rules (which rules fired most often → easiest
  to optimize)
- Top-3 most-deferred edges (which edge_cases the LLM mis-classifies
  most often → candidates for rule promotion)

---

## On-demand commands (future, optional)

`#sai-metrics` channel can take the same `add rule` / `should be`
patterns the sai-eval agent uses, BUT the only valid pattern here is:

- `metrics today` — re-post today's daily without waiting for cron
- `metrics last week` — last week's weekly
- `metrics for <workflow_id>` — single-workflow view
- `metrics cost since <date>` — custom range

Per #16e (guarded interfaces never silent), unrecognised input gets
a friendly "this channel is for metrics queries — try `metrics today`"
reply.

The agent that handles this is a TINY workflow (similar pattern to
sai-eval but smaller tool surface):
- `read_audit_log(date_range, workflow_id?)` — read-only
- `read_eval_metrics(date_range, workflow_id?)` — read-only
- `format_report(scope, period)` — formats Slack blocks

No propose tools (this channel is read-only metrics — operator
can't change anything from it). Two-phase commit doesn't apply.

---

## Where the data comes from (no new infrastructure needed)

Already captured today:

| Data | Source | Format |
|---|---|---|
| Per-invocation cost | `~/Library/Logs/SAI/sai_eval_agent.jsonl` (and equivalents) | JSONL |
| Cascade tier resolution rates | `cron logs + `eval_records.jsonl` | JSONL |
| L1 accuracy | regression run reports + `local_cloud_comparisons.jsonl` | JSONL |
| Disagreement queue depth | `eval/disagreement_queue.jsonl` row count | JSONL |
| Edge cases count | `eval/edge_cases.jsonl` row count | JSONL |
| Pending operator actions | `eval/proposed/*.yaml` count + age | filesystem |
| Errors (24h) | `~/Library/Logs/SAI/*.log` grep | text |

The new module is purely a **collector + formatter**, not new data
collection.

---

## Module shape

```
app/skills/sai_metrics/
  skill.yaml                       # workflow_id: sai-metrics
  canaries.jsonl                   # mostly synthetic; rule-tier is small
  edge_cases.jsonl                 # operator-flagged unusual reports
  workflow_regression.jsonl        # tests for the report formatter
  collector.py                     # reads audit logs / jsonl + aggregates
  formatter.py                     # produces Slack-block messages
  scheduler.py                     # daily + weekly entrypoints (called by cron)
```

Triggered via launchd plist:
```
~/Library/LaunchAgents/com.sai.metrics-daily.plist  → 7am daily
~/Library/LaunchAgents/com.sai.metrics-weekly.plist → 7am Monday
```

Both wrap `bash scripts/with_1password.sh python -m app.skills.sai_metrics.scheduler ...`.

---

## Hard rules

1. **Channel-bound.** Posts only to `#sai-metrics` (configured via
   `SAI_SLACK_METRICS_CHANNEL` env var).
2. **Read-only.** Tools are exclusively read_only rights tier. No
   propose tools, no mutations.
3. **Cost cap on the metrics workflow itself.** Default $0.05/day.
   The daily report uses local aggregation only (no LLM calls).
   Weekly report uses local aggregation + ONE LLM call to format
   the narrative summary (~$0.005). On-demand commands use ONE
   LLM call each.
4. **Idempotent reposts.** `metrics today` re-posts the same data;
   posts are NOT cached because the data is cheap to re-aggregate.
5. **Per-workflow opt-out.** A skill manifest can declare
   `observability.metrics_emit: false` to opt out of cost reporting
   (e.g., for a workflow that's intentionally noisy). Default is
   true.

---

## Format choices

Slack-blocks vs text — go with **structured Slack-blocks** for the
header + costs table; **plaintext** for narrative sections.
Trade-off:
- Slack-blocks render nicely on desktop + mobile
- They don't render in operator's Slack notification preview
  (compact mode shows plaintext only)
- → use a `text:` field with the headline ("📊 SAI metrics — daily,
  $0.57 today") so notifications are useful AND the in-channel
  view is rich.

Don't bother with attachment-style colors / fields — Slack
deprecated the attachment API for new uses. Use Block Kit.

---

## Eval contract

Same as any workflow under §33:

- `canaries.jsonl` — synthetic test cases for each report kind
  (daily, weekly, on-demand). Verify the formatter produces
  valid Slack-block JSON.
- `edge_cases.jsonl` — operator-flagged anomalies (e.g., a day
  where cost spiked to $5; format should highlight it).
- `workflow_regression.jsonl` — tests for the metrics agent
  itself (`metrics today` should return today's report;
  `metrics for nonexistent_workflow` should refuse cleanly).

---

## Open questions for operator review

1. **Channel name.** I propose `#sai-metrics` (matches the existing
   `sai-*` pattern). Alternative: `#sai-cost` (narrower) or
   `#sai-dashboard` (broader). Pick one before I build.
2. **Daily cron time.** I propose 7am (matches your existing
   reports). Acceptable?
3. **Weekly cadence.** Monday 7am summary OR Sunday evening?
4. **Cost-spike alerting.** Should the daily post be SUPPRESSED
   on no-news days and only post on:
   - cost > 1.5× 7d avg, OR
   - any workflow accuracy drop > 5%, OR
   - error count > 0 in last 24h
   This makes the channel high-signal. Default I'd propose: post
   daily, but use 🚨 emoji prefix when ANY threshold trips.
5. **Should dashboard observe sai-eval agent's costs separately?**
   It's already in the audit; question is whether to break it out
   in the daily post or roll up.
6. **Multi-week trends.** Worth computing a 30-day rolling? Adds
   ~10s to the daily run. Probably yes; revisit if it's slow.

---

## Effort

~1 session for the bare-bones daily post:
- collector.py + formatter.py + scheduler.py
- launchd plist
- 6-8 unit tests for the aggregator + formatter
- Slack post integration (reuses existing slack-bolt + token from
  runtime.env)

~0.5 additional session for weekly + on-demand commands.

Total: 1.5 sessions to ship. Useful immediately for monitoring;
unblocks "is this workflow burning money?" questions you currently
answer by `tail -f`.

---

## What this is NOT

- **Not a web UI.** Slack-only. If you want a web view later, the
  data backbone is already in JSONL files; build a separate
  Streamlit / static-site tool reading the same logs.
- **Not real-time.** Daily + weekly + on-demand. No "live" stream.
- **Not predictive.** Surfaces what's happened. No forecasting,
  no anomaly detection beyond the simple thresholds.
- **Not a replacement for the regression suite.** Regression
  catches CORRECTNESS regressions; this catches OPERATIONAL drift
  (cost spikes, queue depth, etc.).
