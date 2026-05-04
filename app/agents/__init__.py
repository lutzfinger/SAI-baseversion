"""SAI agent execution planes.

Each module here is a guarded LLM agent: a defined tool surface, an
audit log, and an iteration cap. Agents NEVER mutate state directly —
they call ``propose_*`` tools that stage YAML proposals; mutation
happens only when the operator approves via the existing two-phase
commit (PRINCIPLES.md §9).

Currently shipped:

  - ``sai_eval_agent`` — the execution plane behind ``#sai-eval``.
    Replaces the rigid LLM-fallback parser with a tool-using LLM that
    can search Gmail, list real bucket labels, and propose changes.
"""
