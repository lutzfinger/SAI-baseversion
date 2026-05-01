# SUPPLIERS.md — third-party dependencies & security tracking

This file lists every external library, service, container image, and
upstream tool that SAI depends on. It exists for two reasons:

1. **Security-update auditing.** When a CVE drops for any of the
   suppliers below, we need to know without grepping the codebase.
   Subscribe to the listed advisory feed for each supplier and treat
   updates here as part of the normal commit flow.

2. **Architectural transparency.** Per the standard-libs-first principle (standard
   libraries before custom code), we deliberately favour mature
   libraries over hand-rolled infrastructure. This file is the
   inventory that makes the trade-off auditable: every listed entry
   represents work we chose NOT to build ourselves.

## How to use this file

- Before adding a new dependency, append it here with version range
  + advisory link.
- Every 30 days, sweep the table and check each supplier's recent
  releases for security patches. Bump pinned versions if needed.
- If a supplier is replaced by a different one (e.g. a fork, or a
  pivot to a stdlib equivalent), keep the old entry with `status:
  retired (date)` and link to the replacement.
- When CVEs are published against a listed supplier, file an issue
  here describing the impact and the fix-version we move to.

---

## Python runtime

| Supplier | Used for | Version pin | Source / advisory |
|---|---|---|---|
| **CPython** | language runtime | `python>=3.12` (Docker image: `python:3.12-slim`) | <https://www.python.org/dev/security/> |

## Direct Python dependencies (pyproject.toml)

| Supplier | Used for | Version pin | Source / advisory |
|---|---|---|---|
| `fastapi` | HTTP control-plane API | `>=0.115,<1.0` | <https://github.com/fastapi/fastapi/security/advisories> |
| `uvicorn` | ASGI server fronting fastapi | `>=0.34,<1.0` | <https://github.com/encode/uvicorn/security/advisories> |
| `pydantic` | data validation, schemas | `>=2.10,<3.0` | <https://github.com/pydantic/pydantic/security/advisories> |
| `pydantic-settings` | env-driven config | `>=2.7,<3.0` | <https://github.com/pydantic/pydantic-settings/security/advisories> |
| `pyyaml` | YAML parsing (read) | `>=6.0,<7.0` | <https://github.com/yaml/pyyaml/security> |
| `ruamel.yaml` | YAML editing (round-trip with comments) | `>=0.18,<1.0` | <https://sourceforge.net/p/ruamel-yaml/tickets/> |
| `slack-sdk` | Slack Web API client | `>=3.36,<4.0` | <https://github.com/slackapi/python-slack-sdk/security/advisories> |
| `slack-bolt` | event-driven Slack bot framework | `>=1.20,<2.0` | <https://github.com/slackapi/bolt-python/security/advisories> |
| `google-api-python-client` | Gmail API client | `>=2.170,<3.0` | <https://github.com/googleapis/google-api-python-client/security/advisories> |
| `google-auth-oauthlib` | OAuth flow for Gmail | `>=1.2,<2.0` | <https://github.com/googleapis/google-auth-library-python-oauthlib/security/advisories> |
| `google-auth-httplib2` | HTTP transport for google-auth | `>=0.2,<1.0` | <https://github.com/googleapis/google-auth-library-python-httplib2> |
| `openai` | OpenAI Responses API client | `>=2.30,<3.0` | <https://github.com/openai/openai-python/security/advisories> |
| `langgraph` | DAG orchestration | `>=1.1,<2.0` | <https://github.com/langchain-ai/langgraph/security/advisories> |
| `langgraph-checkpoint-sqlite` | SQLite checkpointing for langgraph | `>=3.0,<4.0` | <https://github.com/langchain-ai/langgraph> |
| `langchain-core` | base abstractions | `>=1.2,<2.0` | <https://github.com/langchain-ai/langchain/security/advisories> |
| `langchain-ollama` | Ollama provider for LangChain | `>=0.3,<2.0` | <https://github.com/langchain-ai/langchain> |
| `langchain-openai` | OpenAI provider for LangChain | `>=0.3.9,<2.0` | <https://github.com/langchain-ai/langchain> |
| `langsmith` | tracing / eval reporting | `>=0.7,<1.0` | <https://github.com/langchain-ai/langsmith-sdk/security/advisories> |
| `pypdf` | PDF text extraction | `>=5.4,<6.0` | <https://github.com/py-pdf/pypdf/security/advisories> |
| `httpx` | HTTP client (Ollama provider, with retries) | `>=0.28,<1.0` | <https://github.com/encode/httpx/security/advisories> |

## Dev / test dependencies

| Supplier | Used for | Version pin |
|---|---|---|
| `pytest` | test runner | `>=8.3,<9.0` |
| `pytest-xdist` | parallel test exec | `>=3.6,<4.0` |
| `mypy` | static type checking | `>=1.15,<2.0` |
| `ruff` | linting + formatting | `>=0.11,<1.0` |

## External services (operator-side)

| Supplier | Used for | Trust mode | Notes |
|---|---|---|---|
| **OpenAI** | cloud LLM tier (gpt-4o-mini, gpt-5.2-pro) | API key, billing | Account-bound. CVEs would be Slack-style account compromises. |
| **Slack** | Ask UI / human-in-the-loop | OAuth bot token + app-level token | Per-workspace install. Bot token scopes documented in `docs/email_classifier_qa.md`. |
| **Google Gmail API** | inbound email + label tagging | per-workflow OAuth | Narrow scopes per `policies/*.yaml` (gmail.readonly, gmail.modify). |
| **LangSmith** (optional) | trace observability | API key | Disabled by default (`SAI_LANGSMITH_ENABLED=false`). |
| **1Password CLI** (optional) | secret resolution from `op://` refs | local CLI auth | Operator's secret store; SAI never embeds secrets. |
| **macOS Keychain** (optional) | secret resolution from `keychain://` refs | macOS-native | Same shape as 1Password. |

## Container images

| Image | Used for | Tag pin |
|---|---|---|
| `python:3.12-slim` | base for SAI control plane | major+minor pinned |
| `ollama/ollama:latest` | local LLM runtime in docker-compose | latest (pin in production) |

Both images are pulled from Docker Hub. Production deployments should
pin to specific digests.

## Local LLM models

| Model | Size | Used for | Notes |
|---|---|---|---|
| `qwen2.5:7b` | ~4.5GB | local_llm tier (default) | Verified reliable for structured-output JSON Schema. |
| `gpt-oss:20b` | ~12GB | (alternative) | Emits empty bodies for many JSON-schema-constrained calls; not recommended. |

Models live in `~/.ollama/models` on the host (or in the
`ollama_models` named volume in the compose setup).

## Standard-library replacements we deliberately stay on

Per the standard-libs-first principle, we do NOT add a library when stdlib covers
the use case. These are documented here so future audits see the
deliberate choice:

| Use case | Library considered | Decision | Reason |
|---|---|---|---|
| HTTP client | `requests` | `httpx` (now used in Ollama provider after the Phase 1 migration) | Migration complete — provides connection pooling, retries, and structured timeouts. |
| Logging | `structlog`, `loguru` | stdlib `logging` | **Phase 3 migration pending** — replace ad-hoc `print()` calls. |
| JSON parsing | `orjson`, `ujson` | stdlib `json` | Performance not currently a bottleneck. |
| File locking | `filelock`, `portalocker` | shell `mkdir <dir>` pattern in launchd scripts | Adequate for single-host single-operator. |
| Hash verification | various manifest libs | stdlib `hashlib` + custom 30-line manifest | Tiny surface, fail-closed semantics; library overhead exceeds value (per the principle exception for tiny well-understood surfaces). |
| Audit log format | various log libs | append-only JSONL written by hand | Operator-greppable, line-oriented; obscuring it inside a library hurts debugging. |

## Custom infrastructure (NOT outsourced — yet)

These are areas where we currently maintain custom code but a library
would likely be better. Tracked here so the ROI of replacement is
visible:

| Custom code | Standard alternative | Status | Notes |
|---|---|---|---|
| LLM provider abstraction (`app/llm/providers/*.py`) | `litellm` | **Phase 2 migration** | Keep our `Provider` Protocol; back it with LiteLLM for the actual vendor calls. |
| AskStore / EvalRecordStore (custom JSONL) | `sqlite3` (stdlib) with WAL | **Phase 2 migration** | JSONL has no atomicity; concurrent writes can corrupt. |
| Slack polling daemon (`scripts/apply_qa_suggestions.py`) | `slack-bolt` Socket Mode | **Phase 1 migration in progress** | 7 self-inflicted bugs already (see PRINCIPLES.md). |

---

## Update procedure

When you bump a dependency:

1. Update the `[project.dependencies]` block in `pyproject.toml`.
2. Update the version pin in this file's matching row.
3. Run `make install` then `make test` to confirm no breakage.
4. If the bump fixes a CVE, link the advisory in the commit message.

When a new CVE is reported against a listed supplier:

1. Open an issue in this repo named `SECURITY: <supplier> <CVE-ID>`.
2. Note the affected version range and the fix version.
3. Schedule the bump within 7 days (critical), 14 days (high), or
   30 days (medium / low).
4. Apply the bump per the procedure above.

---

## Last reviewed

| Date | Reviewer | Outcome |
|---|---|---|
| 2026-05-01 | initial pass | All Python deps current; `gpt-oss:20b` flagged unreliable; `slack-bolt` queued for migration |
