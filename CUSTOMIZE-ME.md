# CUSTOMIZE ME — your SAI customization punch list

This file lists every template in the public starter that you need to
personalize before your SAI install reflects your work, your contacts,
and your habits.

The intended workflow:

1. Clone this repo (the public starter).
2. Create your own private overlay repo (matches the layout in
   `app/runtime/overlay.py` — public files override at the same path).
3. **Hand this file to an LLM** (Claude, GPT, etc.) with the prompt:

   > I want to use SAI for `<your use case>`. Walk through every entry in
   > CUSTOMIZE-ME.md, ask me what each placeholder should be, and write
   > the customized versions into my private overlay at the matching
   > paths. Don't touch the public repo.

4. The LLM iterates through this list, asks you focused questions for
   each template, and writes the answers as files in your private repo.
5. Run `sai-overlay merge --public . --private ../my-sai-private --out ~/.sai-runtime`
   to produce the merged runtime tree SAI loads from.

---

## What to customize

Every entry has:
- **Path** — the file in the public starter to read for the schema/shape
- **Goes private at** — where to write your customized version in the overlay
- **What to fill in** — the placeholders the LLM should ask you about

### Email classification (highest leverage — this is what tags every inbox thread)

| Path (public, schema) | Goes private at | What to fill in |
| --- | --- | --- |
| `prompts/_examples/email/llm-classify.md` | `prompts/email/llm-classify.md` | Your L1 buckets (replace made-up `customers` / `partners` / `personal` / etc. with your actual taxonomy); your few-shot example emails |
| `prompts/_examples/email/llm-classify-gptoss.md` | `prompts/email/llm-classify-gptoss.md` | Same L1 buckets as above (must match); shorter few-shots tuned for the local model |
| `workflows/_examples/newsletter-lane-gmail-tagging.yaml` | `workflows/email-tagging-daily.yaml` | Sender domains per L1 bucket (the keyword baseline); destination Slack channel for digest; schedule |

### Meeting + supervision workflows

| Path | Goes private at | What to fill in |
| --- | --- | --- |
| `prompts/_examples/granola/role-coach.md` | `prompts/coaching/role-coach.md` | Your role taxonomy (e.g. `advisor`, `manager`, `mentor`); rubric per role (what "good" looks like for you in each role) |
| `prompts/_examples/granola/role-classify-local.md` | `prompts/coaching/role-classify-local.md` | Same role taxonomy as `role-coach.md`; few-shot examples |
| `prompts/_examples/granola/role-classify-cloud.md` | `prompts/coaching/role-classify-cloud.md` | Same role taxonomy; richer rubric per role |
| `prompts/_examples/meeting/cornell-ta-supervision.md` | `prompts/meeting/team-supervision.md` | Your delegate roster (`config/team_members.yaml`); follow-up horizons per delegate; escalation channel |
| `workflows/_examples/meeting-followup-intake-draft-only.yaml` | `workflows/meeting-followup.yaml` | Your meeting-notes source (Granola, Otter, Fireflies, etc.); the Slack channel for follow-ups; approval policy |
| `workflows/_examples/meeting-supervision-review-daily.yaml` | `workflows/team-supervision-daily.yaml` | Delegate roster path; supervision schedule; the channel where reminders go |

### Research + watchlist

| Path | Goes private at | What to fill in |
| --- | --- | --- |
| `prompts/_examples/people_interest/weekly-search.md` | `prompts/research/weekly-search.md` | The fields per watchlist entry; output destination; time window |
| `workflows/_examples/people-of-interest-weekly.yaml` | `workflows/people-research-weekly.yaml` | Your watchlist (`config/watchlist.yaml`); search frequency; output channel |

### Other workflow templates worth a look

These are simpler — usually one or two values per file (a Slack channel,
an email recipient, a schedule). The LLM can sweep them quickly.

| Path | Goes private at | What to fill in |
| --- | --- | --- |
| `workflows/_examples/newsletter-summary-daily.yaml` | `workflows/newsletter-summary.yaml` | Newsletter sender list; summary destination; schedule |
| `workflows/_examples/newsletter-unsubscribe-daily.yaml` | `workflows/newsletter-unsubscribe.yaml` | Your unsubscribe allowlist / blocklist |
| `workflows/_examples/sai-email-interaction.yaml` | `workflows/sai-email-interaction.yaml` | Your agent's identity email; allowed senders |
| `workflows/_examples/invoice-forward-quickbooks-daily.yaml` | `workflows/invoice-forward.yaml` | Your QuickBooks account; sender allowlist |
| `workflows/_examples/travel-operation-intake.yaml` | `workflows/travel-intake.yaml` | Your calendar; approval channel; intake email |
| `workflows/_examples/travel-operation-execution.yaml` | `workflows/travel-execution.yaml` | Calendar policy; approval gate channel |
| `workflows/_examples/linkedin-archive-request.yaml` | `workflows/linkedin-archive.yaml` | Your LinkedIn export schedule; processed-data destination |
| `workflows/_examples/granola-note-review-hourly.yaml` | `workflows/notes-review-hourly.yaml` | Notes source; review destination |
| `workflows/_examples/granola-role-score-daily.yaml` | `workflows/role-score-daily.yaml` | Role taxonomy; scoring destination |
| `workflows/_examples/granola-role-score-weekly.yaml` | `workflows/role-score-weekly.yaml` | Same — weekly aggregate |
| `workflows/_examples/repeated-error-review-daily.yaml` | `workflows/error-review-daily.yaml` | Error log path; review channel |
| `workflows/_examples/cornell-course-ta-supervision.yaml` | `workflows/team-supervision.yaml` | Delegate config; schedule |
| `workflows/_examples/ai-audio-brief-daily.yaml` | `workflows/audio-brief-daily.yaml` | Sources; podcast-feed destination |
| `workflows/_examples/daily-cost-report-daily.yaml` | `workflows/cost-report-daily.yaml` | Billing accounts; report channel |
| `workflows/_examples/daily-token-usage-report-daily.yaml` | `workflows/token-usage-report-daily.yaml` | OpenAI org; report channel |
| `workflows/_examples/sample_email_messages.json` | `eval/sample_email_messages.json` | (eval dataset — fill in 50–100 real emails per L1 bucket once you have email classification dialed in) |

### Policies

The 24 policy files in `policies/_examples/` are mostly *threshold* and
*allowlist* configurations. Per file you'll fill in:

- Allowed Slack channels for output
- Allowed sender domains
- Allowed recipient domains  
- Time-of-day allowlists for outbound actions (e.g. "no posts after 10pm")
- Approval requirement (`allow` vs `approval-required`)

The full policy list:

```
policies/_examples/contact_investigation.yaml
policies/_examples/cornell_ta_supervision.yaml         → policies/team-supervision.yaml
policies/_examples/daily_token_usage_report.yaml
policies/_examples/email_tagging.yaml
policies/_examples/email_triage.yaml
policies/_examples/granola_note_review.yaml            → policies/notes-review.yaml
policies/_examples/meeting_decision.yaml
policies/_examples/meeting_followup_draft_only.yaml
policies/_examples/newsletter_summary.yaml
policies/_examples/newsletter_unsubscribe.yaml
policies/_examples/sai_email_interaction.yaml
policies/_examples/travel_operation_execution.yaml
... (and 12 more — see `ls policies/_examples/`)
```

Default mode for any first-deploy policy: **`approval-required`**.
Downgrade specific paths to `allow` only after you've watched the
workflow run cleanly under approval for a week.

### Config + identity

| Path | What to fill in |
| --- | --- |
| `~/.config/sai/runtime.env` (lives outside both repos) | Env-var pointers to your real OAuth token paths, your real keychain references — never literal secrets |
| `config/people_of_interest.yaml` (private) | Watchlist for the people-research workflow |
| `config/team_members.yaml` (private) | Delegate roster for supervision workflows |
| `config/lutz_role_taxonomy.yaml` → rename to your own | Role taxonomy used by all coaching/classifier prompts |
| `config/your-domain-allowlist.yaml` (create per workflow that needs it) | Sender / recipient domains for outbound actions |

### Optional: secrets in 1Password

The framework supports `op://` references in env files (for 1Password CLI)
and `keychain://` references (for macOS Keychain). The actual secrets
never live in either repo. See `app/shared/runtime_env.py` for the
loader. Format example (in `~/.config/sai/runtime.env`):

```
OPENAI_API_KEY="keychain://sai/openai_api_key"
SAI_LANGSMITH_API_KEY="op://Personal/SAI/langsmith_api_key"
```

---

## Once you've customized

```sh
# Build the merged runtime
sai-overlay merge \
  --public  ~/sai-public \
  --private ~/my-sai-private \
  --out     ~/.sai-runtime

# Verify
sai-verify --runtime ~/.sai-runtime

# Run
sai-api  # (or however your scheduled jobs invoke SAI)
```

Verification (`sai-verify`) re-hashes every file against the merge
manifest. Tampering or unregistered files fail-closed before the
control plane even starts.
