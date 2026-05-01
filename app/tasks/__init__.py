"""Public TaskFactory namespaces.

The directory contains:
  - I/O schemas (`*_io.py`) that are PUBLIC because they're referenced by
    `registry/tasks/*.yaml` and a private TaskFactory needs to import them.
  - No factories themselves. Factories live in private — they wire real
    OAuth tokens, real prompts, real channel names. Public ships the
    mechanism (Tier protocol + TaskConfig schema), private ships the values.
"""
