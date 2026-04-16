# SAI Baseversion

SAI Baseversion is a small starter repo for building governed agent workflows
with a narrow, shareable surface area.

The starter keeps a few core principles:

- prompts, policies, and workflows live outside application code
- policy and approval enforcement stay in the control plane
- audit logs and learning datasets are append-only
- connector scopes stay narrow
- write actions are explicit and reviewable

## Included Workflows

1. `newsletter-identification-gmail`
   - reads Gmail messages
   - classifies each message as `newsletter`, `general`, or `other`
   - writes evaluation rows
   - does not modify the mailbox

2. `newsletter-identification-gmail-tagging`
   - runs the same classification flow
   - applies starter Gmail labels after classification

3. `starter-email-interaction`
   - reads operator emails sent to the configured SAI alias
   - extracts safe document text from supported attachments
   - replies with an answer, a follow-up question, or an approval-backed plan
   - can execute a bounded action only after approval

## Repo Layout

- `app/control_plane`
  - workflow orchestration, policy enforcement, and run execution
- `app/connectors`
  - Gmail and Slack integrations
- `app/workers`
  - the newsletter classifier worker and the email interaction worker
- `app/learning`
  - evaluation datasets and reusable fact memory
- `app/observability`
  - audit logging, run storage, and task tracking
- `registry`
  - tool, task-kind, and effect-class metadata
- `workflows`
  - checked-in workflow definitions
- `policies`
  - checked-in connector and approval policies
- `prompts`
  - checked-in prompts and prompt locks

## Running Locally

Start the API:

```bash
uvicorn app.main:app --reload
```

Useful endpoints:

```bash
curl http://127.0.0.1:8000/api/healthz
curl http://127.0.0.1:8000/api/workflows
curl -X POST http://127.0.0.1:8000/api/workflows/newsletter-identification-gmail/run
```

Optional helper commands:

```bash
make auth-newsletters
make auth-newsletter-tags
make auth-sai-email
make run-newsletters
make run-newsletter-tags
make run-sai-email
```

## Onboarding

This starter is meant to be wired up with local secrets in `.env` and
policy-checked workflow config.

Credential map:

- Gmail: installed-app OAuth client plus local token files. No Google service
  account is used in this starter.
- Slack: bot token for controlled posts. No Slack service account concept is
  used here.
- OpenAI: API key for cloud classification and planning.
- LangSmith: optional API key for tracing.
- Ollama: local model host, no token required by default.

Before you authenticate anything:

1. Copy `.env.example` to `.env`.
2. Set `SAI_USER_EMAIL` to your operator mailbox.
3. Set `SAI_ALIAS_EMAIL` to the mailbox the starter should monitor for
   `starter-email-interaction`.
4. Replace the placeholder email allowlists in
   `policies/starter_email_interaction.yaml`.
5. Replace the placeholder Slack channel allowlist in
   `policies/starter_email_interaction.yaml` if you plan to allow Slack posts.

### Gmail OAuth

Use a Google Cloud OAuth client for a desktop app. Do not create a Google
service account for this repo. The current Gmail connector uses user-consented
OAuth tokens and stores them locally under `logs/`.

1. In Google Cloud, create or choose a project.
2. Enable the Gmail API for that project.
3. Configure the OAuth consent screen.
4. Create an OAuth client ID for a desktop app.
5. Download the client JSON and point `SAI_GMAIL_CREDENTIALS_PATH` at it in
   `.env`.

The repo also supports the advanced path of setting
`SAI_GMAIL_CLIENT_ID` and `SAI_GMAIL_CLIENT_SECRET` directly, but the
downloaded desktop client JSON is the easiest path.

Then authenticate each Gmail workflow explicitly:

```bash
make auth-newsletters
make auth-newsletter-tags
make auth-sai-email
```

Those commands open a browser and write workflow-compatible token files under
`logs/`. Leave `SAI_GMAIL_TOKEN_PATH` blank unless you deliberately want one
shared token file. The workflows use different Gmail scopes, so separate token
files are the safer default.

Scope expectations:

- `newsletter-identification-gmail`: `gmail.readonly`
- `newsletter-identification-gmail-tagging`: `gmail.modify`
- `starter-email-interaction`: `gmail.readonly` and `gmail.send`

Advanced Gmail setup:

- If you want non-interactive local runs, you can set
  `SAI_GMAIL_REFRESH_TOKEN` together with `SAI_GMAIL_CLIENT_ID` and
  `SAI_GMAIL_CLIENT_SECRET`.
- The easiest way to obtain that refresh token is to run one interactive auth
  flow first, then inspect the local token JSON that the repo writes under
  `logs/`.

### Slack

Slack is only needed if you want approval-backed Slack posts from the
`starter-email-interaction` workflow.

1. Create a Slack app for your workspace.
2. Add bot scopes that cover the actions you allow. For the current starter,
   that usually means `chat:write`. If you want the repo to resolve a channel
   by name instead of hard-coding a channel ID, also add the read scopes Slack
   requires for the channel types you use, such as `channels:read` for public
   channels and `groups:read` for private channels.
3. Install the app to the workspace.
4. Copy the bot token into `SAI_SLACK_BOT_TOKEN`.

Optional Slack settings:

- `SAI_SLACK_ALLOWED_USER_IDS` is only needed if you later allow direct
  messages by policy.
- `SAI_SLACK_APP_TOKEN` and `SAI_SLACK_SIGNING_SECRET` are not used by the
  current starter because it does not run Slack Events or Socket Mode.

### OpenAI

Set `SAI_OPENAI_API_KEY` in `.env` for the cloud classifier and the starter
email planner. If you are using an OpenAI-compatible endpoint, also set
`SAI_OPENAI_BASE_URL`.

### LangSmith

LangSmith is optional.

1. Create a LangSmith API key.
2. Set `SAI_LANGSMITH_ENABLED=true`.
3. Set `SAI_LANGSMITH_API_KEY`.
4. Optionally set `SAI_LANGSMITH_PROJECT`,
   `SAI_LANGSMITH_ENDPOINT`, and `SAI_LANGSMITH_WORKSPACE_ID`.

### Local Ollama

If you want the local classifier path, run Ollama locally and make sure
`SAI_LOCAL_LLM_HOST` and `SAI_LOCAL_LLM_MODEL` match your local setup.

## LangChain And LangSmith

The starter now uses LangChain for structured LLM calls:

- `ChatOllama` for the local classifier path
- `ChatOpenAI` for cloud classification and email planning

LangSmith tracing is opt-in and disabled by default. When enabled, model prompts,
inputs, and outputs may be sent to LangSmith for debugging and observability.
Configure it with the optional `SAI_LANGSMITH_*` settings in `.env.example`.

## Design Notes

- Registry says what exists.
- Workflows say how the system is assembled.
- Policies say what it is allowed to do.
- The control plane enforces approvals and auditability.
- Workers stay narrow and connectors stay bounded.

## Docs

- `docs/architecture.md`
- `docs/system_inventory.md`
