# Architecture

SAI Baseversion keeps the shape of a governed workflow system while limiting the
feature set to a small starter surface.

## Layers

1. Control plane
   `app/control_plane/runner.py` loads workflows, policies, and prompts,
   executes runs, enforces approval rules, writes audit events, and persists
   task state.

2. Registry layer
   `registry/tools.yaml`, `registry/task_kinds.yaml`, and
   `registry/effect_classes.yaml` define the starter catalog of tools, task
   kinds, and effect classes.

3. Connectors
   Gmail and Slack connectors live in `app/connectors` and stay intentionally
   narrow.

4. Workers
   `app/workers/newsletter_identifier.py` and
   `app/workers/sai_email_interaction.py` compose tools without owning policy
   decisions.

5. Learning and memory
   `app/learning/email_eval_dataset.py`,
   `app/learning/sai_email_dataset.py`, and
   `app/learning/fact_memory.py` hold append-only learning data and reusable
   facts.

6. Observability
   `app/observability/audit.py`, `app/observability/run_store.py`, and
   `app/observability/task_plane_models.py` provide auditability and durable
   operational state.

## Active Workflows

- `newsletter-identification-gmail`
- `newsletter-identification-gmail-tagging`
- `starter-email-interaction`

## Core Principles

- prompts, policies, and workflows live outside application code
- approval enforcement stays centralized
- logs and datasets are append-only
- task state and fact memory are durable
- write-side effects require explicit policy support and, when needed, approval
