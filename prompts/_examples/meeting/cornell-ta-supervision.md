---
prompt_id: example_team_member_supervision
version: "1"
description: Classify a multi-party email thread for follow-up timing — used when you've delegated something to a team member and want to track when to nudge or escalate.
---

# What this prompt does

Reads one email thread where you and a delegate are both involved (e.g.
you're a manager, they're a team member; or you're a professor, they're
a TA; or you're a founder, they're a contractor). Returns:

- Whether the delegate has responded recently
- Whether the thread needs your direct intervention
- When to follow up if no movement happens

The intended workflow: SAI tags threads with a "supervision" label,
this prompt runs daily, and when a thread crosses a follow-up threshold
SAI sends you a Slack reminder.

# How to customize for your use case

1. **Delegate roster** — `config/team_members.yaml` (private) lists who
   counts as a "delegate" for this workflow. Format:
   `[name, email, role, supervision_horizon_days]`.
2. **Follow-up cadence** — defaults to 3 days for non-urgent threads,
   1 day when the thread mentions deadlines or external customers.
3. **Escalation channel** — Slack channel for reminders. Defaults to
   `#general` in the example workflow.

This prompt was originally written for academic TA supervision but is
generic over any "I delegated something, when do I check in?" pattern.

---

You review one email thread that already entered the supervision lane
because both the operator and a delegate were on it.

Return:

```
{
  "thread_id": "...",
  "delegate_responded_within_horizon": true | false,
  "needs_operator_action": true | false,
  "next_followup_due": "ISO-8601 datetime or null",
  "reason": "one sentence, max 25 words"
}
```

Rules for `needs_operator_action`:

- `true` if the most recent message asks the operator a direct question
- `true` if the thread is stalled past the supervision horizon and the
  external party is waiting
- `true` if the delegate has explicitly escalated ("I need your input")
- `false` otherwise

Rules for `next_followup_due`:

- If the delegate has responded within the horizon → `null`
- Otherwise → `<delegate's last reply timestamp> + horizon_days`
- If no delegate reply at all → `<thread start> + horizon_days`

# CUSTOMIZE: Made-up example delegates

Your private `team_members.yaml` will look like:

```yaml
team_members:
  - name: "Richard Hendricks"
    email: "richard@pied-piper.example"
    role: "engineer"
    supervision_horizon_days: 3
  - name: "Jared Dunn"
    email: "jared@pied-piper.example"
    role: "operations"
    supervision_horizon_days: 2
```

(Replace with your actual delegates in the private overlay.)
