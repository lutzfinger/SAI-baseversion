"""SAI skill plug-in protocol (PRINCIPLES.md §33).

Every workflow plugged into SAI ships as a **skill** with a single
declarative manifest. The framework loads, validates, and registers
the manifest. If it validates, the workflow inherits cascade + eval +
feedback + observability + safety infrastructure for free. If it
doesn't validate, the framework refuses to register it.

  - ``manifest`` — Pydantic schema for the manifest
  - ``loader``   — load + validate manifests; refuse on hard contract
  - ``sample_echo_skill`` — synthetic skill that demonstrates the
    protocol with no real Gmail/Slack/Anthropic dependency
"""
