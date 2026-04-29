# Threat Model

## Assets to protect

- local prompts, policies, and workflow definitions
- operator credentials and access tokens
- email metadata and message snippets
- approval decisions
- audit logs and workflow artifacts
- LangGraph checkpoint state
- the operator's browser session and local machine state

## Trust boundaries

### Operator boundary

The human operator is the final authority for sensitive actions and policy changes.

### Control plane boundary

The control plane is trusted to orchestrate runs, log events, and enforce policy gates, but not to silently expand privileges.

### Worker boundary

Workers are trusted only within their declared task scope.

### Connector boundary

Connectors touch external systems and should be treated as the highest-risk code path for exfiltration or unintended side effects.

### Background-service boundary

The always-on API and Slack Socket Mode services are trusted to stay local,
health-checked, and operator-visible through `launchd` rather than becoming
opaque autonomous agents.

### Remote observability boundary

Optional LangSmith tracing crosses a network boundary and must be treated as a separate trust decision from local logging.

## Primary threats and mitigations

### Threat: One worker gains broad powers

Mitigations:

- small worker interfaces
- explicit workflow definitions
- policy checks before sensitive actions
- connector-specific scopes

### Threat: Sensitive actions happen without the user noticing

Mitigations:

- approval records stored in SQLite
- audit events for request and decision lifecycle
- deny-by-default for unapproved sensitive actions
- outbound email connectors can be workflow-bounded to explicit recipient
  allowlists so an email-native lane cannot silently reply to arbitrary third
  parties

### Threat: Logs leak too much personal data

Mitigations:

- redaction and minimization before logging
- snippet truncation
- no token or cookie logging
- Gmail OAuth tokens stored in a local token file outside git
- artifact writing kept explicit and typed
- LangSmith tracing disabled by default and redacted before export

### Threat: Prompt or policy drift changes behavior silently

Mitigations:

- prompts and policies stored outside code in versioned files
- prompt hashes recorded on workflow execution
- reflection reports are suggestion-only
- no auto-apply path for policy or prompt changes

### Threat: Browser automation performs unsafe actions

Mitigations:

- browser automation is restricted to narrow workflow-specific cases such as the
  newsletter unsubscribe fallback
- browser actions stay origin-bounded and unsubscribe-only in that path
- visible login flow
- explicit approval or workflow gating before any state-changing interaction

### Threat: Replay and incident review are impossible

Mitigations:

- append-only JSONL audit log
- structured run store in SQLite
- LangGraph checkpointing in SQLite for workflow-state recovery
- replay tests that reconstruct workflow events from logs

### Threat: Background services silently drift or die

Mitigations:

- explicit `launchd` service definitions for the API and Slack listener
- health checks and heartbeat files
- `make services-status` and `make slack-status` for operator-visible checks
- no hidden fallback to an unmanaged daemon model

### Threat: Remote tracing exfiltrates sensitive data

Mitigations:

- LangSmith is opt-in only
- remote traces use the same redaction policy as local logs
- local audit logs remain the primary system of record
- workflow metadata makes it explicit when LangSmith tracing is enabled

## Residual risks

- Local machine compromise can still expose logs or credentials.
- A leaked local Gmail token file could expose read-only mailbox access until revoked.
- A future connector using mis-scoped credentials could expand access if not carefully reviewed.
- Prompt quality issues can still cause poor classifications, even when the system is structurally safe.
- If LangSmith is enabled with an overly permissive redaction policy, sensitive context could still leave the machine.

## Safety posture for personal-account systems

For systems like LinkedIn or personal WhatsApp:

- assume human-in-the-loop operation
- avoid silent background activity
- prefer visible, user-driven sessions
- require explicit approvals for any outbound action
- do not design around bypassing platform rules or account safeguards
