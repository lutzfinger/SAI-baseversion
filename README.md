# SAI — Stack of AI for personal automation

SAI is an open framework for running your personal AI automations on
your own machine, with **eval data as First Citizen**, not the chat log.

## Are you frustrated with personal AI agents?
I am. I want to develop my agents as easy as I can chat with Claude. But once I am happy. 
I want to rely on this agent and know that it learns and does not regress. 

## What a real personal-agent system needs

Three things:

1. **Workflow completion.** The system should measure whether the
   *job* was actually done, not just whether the tool ran.
2. **Outcome evals.** Approvals, edits, rejections, and overrides
   should become structured feedback.
3. **Separation between design and execution.** The entity that
   *designs* the workflow should not be the only entity *judging*
   whether it succeeded.

This is why we built SAI.

SAI started two years ago as a RAG version of myself for my Cornell
course. It helped students code, discuss ideas, and work through
course material. Since then, we have been turning it into the
framework I kept wishing existed.

The idea is simple:

- Use **Claude Co-Work** (or another flexible interface) to
  *design* the skill.
- When the workflow is ready, hand it to **SAI**.
- SAI runs it, tracks the outcomes, stores the evals, and comes
  back to discuss quality and improvements.

The flexible system helps you design. SAI helps you execute,
observe, evaluate, and improve.

We just launched it in our Cornell & INSEAD workshops. It is early. It will
break. But it is built around the problem I think matters most:
not better prompts, not prettier workflows — **better completion
loops.**

Try SAI connect it. Define your personal workflow via Claude and then let SAI scale it up.
Tell me what you think:
[github.com/lutzfinger/SAI-baseversion](https://github.com/lutzfinger/SAI-baseversion).

---

## Why SAI exists

Every interaction with an AI agent is a labeled training example
waiting to happen — approve, edit, reject, rewrite. Most personal-AI
tools throw it away the moment you close the window.

SAI treats **eval as a first-class citizen**. Every workflow that
plugs into SAI inherits cascade execution + an eval contract +
policy gates + an append-only audit log. A workflow that doesn't
declare its eval datasets cannot ship through the framework. That
is the regression-free guarantee.

### The trust property

In any delegated system the actor and the checker cannot be the
same entity. When you delegate to AI, the actor is a model running
under your name. The checker is you, occasionally. The eval data
and the audit log are the bridge between those two roles.

## What ships

- **Cascade execution.** rules → classifier → local LLM → cloud LLM
  → human. Early-stop on confidence; expensive tier is the long
  tail, not the default. Build-time the cascade goes the other way:
  new tasks start at cloud LLM and graduate downward as eval data
  accumulates, every graduation human-approved on P/R against
  ground truth.
- **EvalDataset abstraction**, five concrete subclasses today:
  - `CanaryDataset` — one synthetic case per rule, hard-fail on miss
  - `EdgeCaseDataset` — real cases the LLM had to reason on,
    soft-capped (default 50) to force curation
  - `WorkflowDataset` — catches drift in a workflow's plumbing
    (system prompt, regex, tool wiring)
  - `DisagreementDataset` — local-vs-cloud disagreements awaiting
    batched operator triage
  - `TrueNorthDataset` — uncapped historical record, run weekly
    with a hard cost cap
- **Skill plug-in protocol.** Every workflow ships as a `skill.yaml`
  manifest declaring identity, trigger, cascade, tools, eval,
  feedback channel, outputs, policy. Validates at load time;
  framework refuses to register a skill missing any of the three
  required eval kinds.
- **Public framework, private overlay.** The mechanism is open and
  shareable; the values (taxonomies, prompts, channel names, OAuth
  tokens, eval data) stay yours. Two repos. Runtime merges them at
  startup with file-level override (no per-key YAML merging — that
  silently changes behavior).
- **Policy gate before every side effect.** Workers don't decide
  their own permissions. The gate reads YAML.
- **Per-workflow OAuth scopes.** `gmail.readonly` for the
  classifier, `gmail.modify` for the labeler, `gmail.send` only
  for senders. No superuser token shared across the system.
- **Reality-only ground truth.** Tier predictions never count as
  ground truth. Even a unanimous cascade leaves
  `is_ground_truth=False` until reality confirms (direct user
  action, explicit Slack reply, co-work session approval). Models
  agreeing with each other is not signal.
- **Append-only audit.** Every gate decision, connector call,
  approval transition, verification failure writes one JSONL row.
  The answer to "what did the system do this week" lives in one
  place.
- **Hash-verified loading.** SHA-256 every merged file, fail closed
  on mismatch. When you see "workflow X had 87% pass rate in March"
  you know workflow X was the same file all month.
- **Reflection may suggest, never auto-apply.** The system can
  propose prompt revisions based on eval data. It cannot apply
  them. The design surface (Co-Work) and the execution surface
  (Claude Code + the running daemon) are separated by a hash and a
  check-in (#33b).
- **sai-eval Slack agent.** Operator-tunable feedback channel.
  Type `add rule: <sender> → <bucket>` or
  `<message-ref> should be <bucket>`; the bot stages a proposal,
  you react ✅ to apply.

## Quick start for strangers

Two paths.

### Wizard-guided (recommended)

```sh
git clone https://github.com/lutzfinger/SAI-baseversion ~/SAI-baseversion
cd ~/SAI-baseversion
```

Open Claude Code or Co-Work, paste the contents of
[docs/onboarding_wizard_prompt.md](docs/onboarding_wizard_prompt.md)
as the first message. The wizard walks you from zero → "first email
tagged" in a single ~30-min session: detects your platform, picks a
secret backend (1Password / Keychain / literal `.env`), wires Gmail
OAuth, defines your first L1 taxonomy, runs a smoke test on 5 real
emails, optionally installs a launchd cron + the sai-eval Slack
agent.

End state: SAI tags your inbox automatically, with a feedback
channel for taxonomy corrections.

### Manual

Need Python 3.12+ and (recommended) Ollama for the local LLM tier.

```sh
git clone https://github.com/lutzfinger/SAI-baseversion ~/SAI-baseversion
cd ~/SAI-baseversion
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env       # then edit with your secret references
```

Configure secrets in `~/.config/sai/runtime.env` (see
[docs/onboarding_wizard_prompt.md](docs/onboarding_wizard_prompt.md)
Q3 for the template). Run an interactive Gmail OAuth flow to mint
the token. Define your taxonomy in
`prompts/email/keyword-classify.md` (private overlay). Run the
smoke test:

```sh
.venv/bin/python -m scripts.backtest_email_classifier --limit 5 --dry-run
```

## Operator's day-to-day commands

Once installed, you mostly interact through Slack `#sai-eval` (or
the local HTTP chat fallback at `http://127.0.0.1:8765`). The
command line is for occasional checks.

```sh
# Run the full regression suite (cascade + canonical loaders + skills)
bash scripts/run_regression_suite.sh

# Boundary linter — catches operator data leaking into public repo
.venv/bin/python scripts/boundary_check.py

# Cascade an email manually (dry-run)
.venv/bin/python -m scripts.backtest_email_classifier --limit 10 --dry-run

# Cost report for the last 7 days
.venv/bin/python -m scripts.sai_cost --days 7

# Eval pass-rate report
.venv/bin/python -m scripts.sai_metrics
```

Slack `#sai-eval` accepted patterns:

- `add rule: <sender|domain> → L1/<bucket>` — adds a deterministic
  rule. Stages a proposal; react ✅ to apply.
- `<message_ref> should have been L1/<bucket>` — adds an LLM
  teaching example (edge_cases). Stages a proposal; react ✅ to apply.

Anything else gets a friendly out-of-scope reply listing the
accepted patterns.

## Building a new workflow

Skills are designed in **Co-Work** through back-and-forth with you,
then handed to **Claude Code** for execution (#33b — Co-Work
designs, Claude Code executes; no design changes happen in Claude
Code).

1. Open Co-Work. Paste
   [docs/cowork_skill_creator_prompt.md](docs/cowork_skill_creator_prompt.md)
   as the system prompt.
2. Walk through the skill-creator's Q1–Q9. It emits four files:
   `skill.yaml`, `canaries.jsonl`, `edge_cases.jsonl`,
   `workflow_regression.jsonl`, plus a `runner.py` skeleton.
3. Drop the emitted directory at
   `$SAI_PRIVATE/skills/incoming/<draft_id>/`.
4. Hand to Claude Code: it runs `validate_skill_manifest`, hashes
   any new prompts, then promotes to
   `$SAI_PRIVATE/skills/<workflow_id>/` once green.
5. Re-merge the overlay (`make overlay-merge`); the running bot
   picks up the new skill on next reload.

The framework refuses to register a skill that doesn't declare all
three required eval datasets (canaries + edge_cases + workflow).
That's #16d.

## Repo layout

| Path | What |
|---|---|
| `app/cascade/` | Public framework cascade runner — handler registry, `run_cascade`, audit log |
| `app/canonical/` | Reusable canonical-memory loaders (courses, TAs, sender_validation, crisis_patterns, text_sanitization, reply_validation, patterns) |
| `app/llm/providers/` | Vendor-agnostic Provider abstractions (Anthropic JSON + Messages, OpenAI Responses, Gemini, Ollama) |
| `app/llm/registry.py` + `config/llm_registry.yaml` | LLM registry — code references models by ROLE, never by literal model id |
| `app/eval/` | EvalDataset abstraction + 5 concrete subclasses |
| `app/skills/` | Skill plug-in protocol — `manifest.py` (Pydantic schema), `loader.py` (validator), `proposal_intake.py`, `sample_echo_skill/` (minimal valid example) |
| `app/runtime/` | Overlay merge, hash-verified loader, AI stack tiers (incl. second-opinion gate) |
| `app/agents/` | sai-eval Slack agent — bounded tool surface, `extra="forbid"` Pydantic input models |
| `app/connectors/` | Gmail, Slack, calendar — narrow API scopes |
| `app/control_plane/` | Policy enforcement + approvals |
| `prompts/` | Hash-locked prompt files (loader fails closed on mismatch) |
| `config/` | Operator-editable YAML (runtime tunables, channel registry, LLM registry) |
| `eval/` | Public eval placeholders + a synthetic skill_creator regression — operator's real eval data is private |
| `scripts/` | Boundary linter, regression suite, canary regenerator, OAuth helpers, cost + metrics CLIs |
| `tests/` | Unit + integration tests (586 passing, 0 boundary violations) |

## Principles + further reading

- [PRINCIPLES.md](PRINCIPLES.md) — durable rules. Read this once
  early to save a lot of confused debugging later.
- [docs/architecture.md](docs/architecture.md) — system architecture
- [docs/memory_architecture.md](docs/memory_architecture.md) — the
  4-tier memory model
- [docs/cowork_skill_creator_prompt.md](docs/cowork_skill_creator_prompt.md)
  — paste-as-system-prompt for designing new skills in Co-Work
- [docs/onboarding_wizard_prompt.md](docs/onboarding_wizard_prompt.md)
  — paste-as-system-prompt for first install

## Compatibility

- macOS first (uses `~/Library/Application Support/SAI/` paths +
  launchd for cron). Linux works for development; cron + secret
  paths need adjustment.
- Python 3.12+
- Optional: Ollama for the local LLM tier; 1Password CLI for
  service-account secret resolution.

## Contributing

The framework (mechanisms) is the open part. Your taxonomy, prompts,
channel names, secrets, eval data — those are yours, not the
framework's. Keep operator data out of pull requests; the boundary
linter (`scripts/boundary_check.py`) catches the obvious leaks.

For anything beyond a tiny docs or typo change, please open an
Issue first so the contribution can be aligned on scope and
direction before implementation. See [CONTRIBUTING.md](CONTRIBUTING.md).
