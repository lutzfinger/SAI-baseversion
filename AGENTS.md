# Repository Agent Guide

This repo is designed for small, scoped agents and human reviewers working together.

## Operating rules

- Keep prompts, policies, and workflows outside application code.
- Treat the control plane as the enforcement layer for policy and approvals.
- Do not add side effects to workers without a policy entry and approval path.
- Keep logs append-only and minimize sensitive data by default.
- Reflection may suggest improvements but may never auto-apply code, prompt, or policy changes.
- Prefer official APIs over browser automation whenever a supported integration exists.
- For consumer communication flows, design for visible human-in-the-loop operation only.

## Engineering expectations

- Favor explicit Python modules over meta-framework magic.
- Keep control-plane policy and audit logic explicit even when workflow execution uses LangGraph internally.
- Put workflow step orchestration in `app/graphs/` and keep `app/workers/` as thin facades over that runtime.
- Add tests with each new workflow or connector.
- Update `README.md` and `docs/` when behavior or trust boundaries change.
- Use mocks and fixtures before introducing live integrations.
- Keep workers narrow and connector scopes minimal.

## Review checklist

- Does the change expand permissions or scopes?
- Does it add a new external side effect?
- Does it log more user data than necessary?
- Does it preserve append-only auditability?
- Does it keep approval enforcement centralized?

If any answer is unclear, stop and tighten the design before merging.
