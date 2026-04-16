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

## Design Notes

- Registry says what exists.
- Workflows say how the system is assembled.
- Policies say what it is allowed to do.
- The control plane enforces approvals and auditability.
- Workers stay narrow and connectors stay bounded.

## Docs

- `docs/architecture.md`
- `docs/system_inventory.md`
