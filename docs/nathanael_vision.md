# SAI: Vision and Roadmap

**Author:** Nathanael  
**Date:** 2026-05-05  
**Status:** Living document — updated as the system evolves

---

## Vision

SAI started as an email triage runtime. That's not what I'm building toward.

The goal is a dynamic, multi-purpose personal agent that learns from my feedback over time — not a static automation pipeline that runs the same if/elif logic forever. What I want is closer to what NousResearch is attempting with Hermes: a general-purpose agent that improves from experience, develops preferences, and can be trusted with novel tasks. The difference is I want it grounded in production-grade operational discipline that Hermes doesn't have. SAI has that foundation. Now I'm extending it.

The bet: combining SAI's eval-first, policy-gated, audit-trailed architecture with a real RL-from-human-feedback loop produces something neither a pure research agent nor a standard automation tool achieves — a personal agent that is reliable enough to trust with real tasks *and* capable of getting better at them.

---

## What SAI Is Today

SAI is an **email-first automation runtime** built on a cascade execution model. It's opinionated, production-oriented, and built to be extended. Here's the honest baseline.

### Cascade Execution

Every task enters the cheapest tier that can handle it:

1. **Rules** — deterministic, free, microseconds
2. **Classifier** — small ML model, milliseconds, near-free
3. **Local LLM** — Ollama / llama.cpp on local hardware
4. **Cloud LLM** — vendor-pluggable (OpenAI, Anthropic, Gemini)
5. **Human** — Slack ask via `#sai-eval`, asynchronous, decisive

The runtime cascades upward only when needed. Build time cascades downward: new tasks start at cloud LLM and graduate to cheaper tiers as eval data accumulates.

### Eval-First Discipline

Every skill must ship with three eval datasets before it runs in production:

- **Canaries** (`canaries.jsonl`) — hard-fail regression tests; if these break, the skill is blocked
- **Edge cases** (`edge_cases.jsonl`) — soft-fail accuracy checks with a max acceptable P/R drop
- **Workflow regression** (`workflow_regression.jsonl`) — end-to-end scenario coverage

Tier graduation (cloud → local → classifier → rules) requires human approval, gated on precision/recall clearing thresholds against eval ground truth. Eval is not a post-hoc check; it is the primary loop the system is designed to grow.

### Human-in-the-Loop

Human oversight lives in Slack (`#sai-eval`). The system sends approval requests, captures corrections, records preference signals. Approvals gate external side effects. Corrections feed the learning layer. Nothing writes to the world without passing the control plane.

### Connectors

- Gmail (read, label, draft reply)
- Slack (approval asks, notifications)
- Google Calendar
- LinkedIn browser automation (approval-gated)
- Google Sheets browser connector

### Skill Plug-in Protocol

Skills are declared via `skill.yaml` manifests. Each manifest specifies identity, trigger, cascade tiers, tool bindings, eval datasets, feedback channels, outputs, policy constraints, and observability settings. The sample skill at `app/skills/sample_echo_skill/` is the canonical reference implementation.

### Operational Guarantees

- Append-only audit log — nothing vanishes silently
- Hash-verified prompt loading — prompts can't be silently swapped
- Policy gates — workers don't decide their own permissions
- Fail-closed defaults — missing auth, hash mismatch, unknown action all refuse

---

## Nathanael's Additions

These are extensions I've built on top of the base framework:

**LinkedIn Referral Outreach Workflow** — approval-gated, browser-based referral message workflow. Lives at `workflows/linkedin-referral-outreach.yaml`. Every send requires human sign-off before the browser connector executes.

**Codex/ChatGPT OAuth Integration** — hosted LLM access via OAuth rather than bare API key. This enables LLM tier usage under an authenticated session model, not just static credential injection.

**LangChain + LangSmith Integration** — LangSmith tracing is wired into the observability layer. Every skill manifest has a `langsmith_project` field. Trace data feeds into evaluation analysis.

**Google Sheets Browser Connector** — extends the connector surface to Sheets for data read/write workflows, particularly useful for eval dataset management and operator outcome tracking.

---

## Roadmap

What's being built next, in priority order:

### 1. RL-from-Human-Feedback Layer (`app/rl/`)

The Slack approval/rejection/edit signals already exist. The work is converting them into training signal:

- **Scalar reward extraction** from approval outcomes, explicit corrections, and preference edits
- **Trajectory capture** — full (prompt, action, outcome) records per invocation
- **Preference pairs** — approved vs. rejected alternatives for the same input
- **Export formats** — DPO, SFT, and PPO-compatible datasets for HuggingFace

The `app/learning/` module has the scaffolding (`operator_outcomes.py`, `training_pipeline.py`, `training_data_notifier.py`). The RL layer formalizes this into a training loop.

### 2. Generic Agent Loop

Current worker dispatch is hardwired if/elif logic. The next architecture is a **pluggable multi-turn agent loop** with tool dispatch — modeled on Hermes's `HermesAgentLoop` but built on SAI's policy-gated execution model. The loop receives a task, selects tools from the manifest's `tools[]` list, executes with approval gates intact, and iterates until the task completes or the iteration cap triggers.

### 3. Benchmark Evals

SWE-Bench style environments: isolated task environments, batch trajectory runner, automated scoring against ground truth. This gives the RL layer something to optimize against beyond Slack approval rate.

### 4. Atropos RL Integration

Live rollout collection → reward scoring → policy gradient updates. Atropos handles the RL training orchestration; SAI supplies the environment (real tasks, real feedback) and the reward signal (human approvals, eval pass rates).

### 5. Sandboxed Execution Backends

Docker and Modal for code execution tasks. Required before the agent loop can be trusted with arbitrary tool calls — isolation before capability expansion.

### 6. Cross-Session Memory

Persistent user modeling beyond per-run fact memory. The `app/learning/` layer already tracks relationship memory, people of interest, and newsletter lane registries. Cross-session memory extends this into a durable user model the agent loop can query.

---

## Architecture Philosophy

What makes this different from Hermes, and why that matters:

**SAI has what Hermes lacks:**
- Production-grade audit trail and policy enforcement
- Eval-first discipline with human-approved tier graduation
- Fail-closed defaults that refuse rather than guess
- Real connector surface (Gmail, Slack, LinkedIn, Calendar)

**Hermes has what SAI lacks:**
- General-purpose tool use and multi-turn reasoning
- RL training loop with benchmark environments
- No assumption about the task domain

The combination is the point. A general-purpose agent without operational discipline is a liability — it will take unintended actions, produce unverifiable outputs, and have no mechanism for systematic improvement. An automation runtime without a learning loop is a static artifact — it does exactly what it was programmed to do and nothing more.

SAI's principles (especially: policy before side effects, fail closed, eval is the purpose) are not constraints to relax as capability grows. They are the foundation that makes it safe to extend capability. The RL layer, the generic agent loop, the benchmark evals — all of these are being built *on top of* the existing policy infrastructure, not in place of it.

---

## How to Contribute / Extend

To add a new workflow or skill:

1. **Create a skill directory** under `app/skills/<your-skill-name>/`
2. **Write a `skill.yaml` manifest.** Required fields:
   - `identity`: `workflow_id`, `version`, `owner`, `description`
   - `trigger`: `kind` (manual, scheduled, event) + `config`
   - `cascade`: ordered list of tiers, each with `tier_id`, `kind`, `config`, `confidence_threshold`, `cost_cap_per_call_usd`
   - `tools`: list of tool bindings (required if any tier is `kind: agent`)
   - `eval.datasets`: at minimum `canaries` (hard-fail) + `workflow` (hard-fail)
   - `policy`: `approval_required`, `cost_cap_per_invocation_usd`, `iteration_cap`, `audit_log_path`
3. **Populate eval datasets** before the skill can run. Canaries must pass. Workflow regression must pass. Edge cases soft-fail at the defined P/R drop threshold.
4. **Add a policy path** for any output with `side_effect != none`. Workers do not self-authorize.

See `app/skills/sample_echo_skill/skill.yaml` for a complete, annotated reference implementation.

See `PRINCIPLES.md` for the seven non-negotiables that every change is judged against. If a proposed change violates a principle, name it and propose a deliberate exception with reasoning — don't silently drift.

External contributions: open an Issue first for anything beyond docs or test additions. See `CONTRIBUTING.md` for scope expectations.
