# Contributing

Thanks for your interest in improving SAI Baseversion.

This repo is intentionally small and opinionated. The easiest way to make a
useful contribution is to align early on scope, safety boundaries, and the
expected implementation shape.

## Before You Start

If you want to make anything beyond a tiny typo or docs fix, open an Issue
first to describe what you want to change.

That is especially helpful for:

- new workflows
- connector changes
- prompt or policy changes
- approval-flow changes
- logging or observability changes
- changes that add external side effects

Opening an Issue first helps avoid wasted effort if the maintainer already has
plans, constraints, or a preferred direction for the change.

## Good First Contributions

The repo is most contributor-friendly for small, scoped changes such as:

- clarifying docs or setup instructions
- adding tests for existing behavior
- tightening typing, linting, or validation
- improving mock-based fixtures
- fixing bugs without expanding permissions or connector scope

## Project Expectations

Please keep these repository rules in mind:

- Keep prompts, policies, and workflows outside application code.
- Keep policy and approval enforcement in the control plane.
- Do not add new write-side effects without an explicit policy path.
- Keep logs append-only and minimize sensitive data by default.
- Prefer official APIs over browser automation.
- Use mocks and fixtures before introducing live integrations.
- Update docs when behavior or trust boundaries change.

## Local Setup

1. Create a Python 3.12 virtual environment.
2. Install the project with dev dependencies.
3. Copy `.env.example` to `.env` if your change needs live integrations.

Example setup:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

## Before Opening a Pull Request

Run the standard local checks:

```bash
make test
make lint
make typecheck
```

If your change touches docs only, say that clearly in the PR.

If your change touches live integrations:

- avoid committing secrets, token files, or local database files
- document any new required env vars in `.env.example`
- document any new setup steps in `README.md`

## Pull Request Checklist

Before submitting, please confirm:

- the change is scoped and explained clearly
- tests were added or updated when behavior changed
- `README.md` and `docs/` were updated if setup or trust boundaries changed
- connector permissions and side effects did not expand accidentally
- no personal or local-only data was introduced into the repo

## What To Include In Your PR

Please include:

- what changed
- why it changed
- how you tested it
- any follow-up work or open questions

For larger changes, linking the Issue is strongly preferred.
