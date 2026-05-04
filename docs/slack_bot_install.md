# Slack bot — install + run runbook

**Audience:** the operator, after the onboarding wizard finishes the
classifier setup. Walks through enabling the sai-eval Slack agent so
the operator can teach the system from Slack instead of Claude Code.

**Paste-safe:** every code block below is a SINGLE command (or
single heredoc) with NO `#` comments inside — zsh in default
configuration treats `#` at the start of an interactive line as
"command not found", so any comment-line in a multi-line paste
breaks the paste. Narrative goes above each block, never inside.

---

## Setup placeholders (run once per shell)

These two env vars make every command below copy-paste-safe and
portable. Set them in your current shell before anything else.

```bash
export SAI_PRIVATE_REPO="$HOME/<your-private-overlay-dir>"
```

```bash
export SAI_RUNTIME="$HOME/.sai-runtime"
```

Replace `<your-private-overlay-dir>` with whatever you cloned the
private overlay into (e.g. the dir holding `scripts/with_1password.sh`
and `scripts/launchd/com.sai.slack-bot.plist`). If you don't have
a private overlay yet, the slack-bot install isn't applicable —
finish the onboarding wizard's overlay step first.

---

## Prerequisites

You need:
- Bot token (`xoxb-...`) with scopes: `chat:write`,
  `channels:history`, `groups:history`, `reactions:read`,
  `users:read`, `conversations.connect:write` (Socket Mode).
- App-level token (`xapp-...`) with scope: `connections:write`.
- A channel registered in `config/channel_allowed_discussion.yaml`
  (default: `sai-eval`) with the bot invited.
- 1Password items for both tokens (or Keychain entries).

If you don't have any of these, follow the Slack-app-creation
section in `docs/onboarding_wizard_prompt.md` first.

---

## Step 1 — verify the secrets resolve

This proves your `runtime.env` has the right `op://` references and
that the service-account token is in place. Run from the merged
runtime root:

```bash
cd "$SAI_RUNTIME" && "$SAI_PRIVATE_REPO/scripts/with_1password.sh" "$SAI_RUNTIME/.venv/bin/python" -c "import os; print('SAI_SLACK_BOT_TOKEN starts with:', os.environ.get('SAI_SLACK_BOT_TOKEN','')[:5]); print('SAI_SLACK_APP_TOKEN starts with:', os.environ.get('SAI_SLACK_APP_TOKEN','')[:5])"
```

Expected: prints `xoxb-` and `xapp-` prefixes (no full token). If
either is empty, you have a `runtime.env` problem — either the
`op://` reference is wrong, or `OP_SERVICE_ACCOUNT_TOKEN` isn't
exported, or the wrapper script isn't being used.

---

## Step 2 — smoke test the bot in the foreground

Run the bot interactively to see startup logs + connection events.
This is the right step to validate that the channel registry,
identity gate, and Slack websocket all work BEFORE you set up
launchd.

```bash
cd "$SAI_RUNTIME" && "$SAI_PRIVATE_REPO/scripts/with_1password.sh" "$SAI_RUNTIME/.venv/bin/python" -u -m scripts.slack_bot
```

Expected output: a few startup lines then `bolt-app` socket
connected. Send a test message in the registered eval channel like
`add rule: bob@example.org -> L1/customers` — the bot should reply
with a staged-proposal confirmation.

Type Ctrl-C to stop. If it didn't connect: tokens are wrong.
If it connected but didn't reply: check `SAI_OPERATOR_USER_ID`
matches your Slack user_id and the channel is in
`config/channel_allowed_discussion.yaml`.

---

## Step 3 — install the launchd plist

Single command, paste-safe:

```bash
cp "$SAI_PRIVATE_REPO/scripts/launchd/com.sai.slack-bot.plist" "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist"
```

---

## Step 4 — load the launchd job

```bash
launchctl load -w "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist"
```

The `-w` flag persists the load across reboots. Without `-w` the
job only runs until the next logout.

---

## Step 5 — verify it's running

```bash
launchctl list | grep com.sai.slack-bot
```

Expected: one line with a numeric PID and exit-code 0. If the
PID column shows `-`, the job loaded but isn't running. If the
exit-code column shows non-zero, it ran and failed — check
`$HOME/Library/Logs/SAI/scheduled/launchd_slack_bot.err.log`.

---

## Step 6 — confirm the bot is alive in Slack

Type a message in the registered eval channel and confirm the bot
replies. If it doesn't, check the err log path above for stack
traces.

---

## Common failure modes

**Symptom:** zsh says `command not found: #` when you paste.
**Cause:** you pasted a block that included a `#` comment line.
**Fix:** paste only the actual command line(s); skip any line
that starts with `#`.

**Symptom:** bot connects but ignores all messages.
**Cause:** channel not in `config/channel_allowed_discussion.yaml`,
OR your user_id doesn't match `SAI_OPERATOR_USER_ID`.
**Fix:** check both. The channel registry check happens BEFORE
the dispatch; an unregistered channel is silently ignored.

**Symptom:** `Prompt hash mismatch` in the err log.
**Cause:** someone edited `prompts/agents/sai_eval_agent.md`
without refreshing `prompts/prompt-locks.yaml`.
**Fix:** re-merge the runtime (`sai-overlay merge`) OR refresh
the lock file with the new SHA-256.

**Symptom:** `OP_SERVICE_ACCOUNT_TOKEN` errors.
**Cause:** plist isn't using `with_1password.sh` wrapper, OR
`runtime.env` doesn't have the right keychain reference.
**Fix:** re-run Step 1 to confirm secrets resolve from a shell.

---

## Stop / unload / re-install

To stop the bot temporarily:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist"
```

To re-install after editing the plist (single command, no `#`):

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist" && cp "$SAI_PRIVATE_REPO/scripts/launchd/com.sai.slack-bot.plist" "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist" && launchctl load -w "$HOME/Library/LaunchAgents/com.sai.slack-bot.plist"
```
