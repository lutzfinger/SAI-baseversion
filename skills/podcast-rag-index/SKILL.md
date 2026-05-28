---
name: SAI-podcast-rag-index
description: Atomic SAI skill. Index a directory of transcript .md files (YAML frontmatter, one per episode) into an existing ChromaDB RAG store by delegating to a rag-index-updater-style index.py. Pure mechanism — no operator paths or collection baked in; the caller supplies indexer_scripts / manifest_path directly or via a private config_path. Hash-skips unchanged files. Use whenever you need to index transcripts, add transcripts to a RAG, or embed episodes for semantic lookup.
---

# Trigger manifest — podcast-rag-index (atomic, autonomous)

Code: `skills/podcast-rag-index/`
Manifest: `skill.yaml` (3-tier cascade)
Entry point: `runner.py` → `run(inputs)`

## Cascade plan

```
1. locate_indexer   (rules) → resolve indexer_scripts (input or config_path); fail-closed if unset
2. scan_inputs      (rules) → diff input_dir vs manifest (new+changed only)
3. run_indexer      (rules) → shell to index.py → ChromaDB upsert
```

## Invocation

```bash
.venv/bin/python3.12 \
  skills/podcast-rag-index/runner.py run \
  --inputs-json '{
    "input_dir":   "~/Media/transcripts/<show-slug>",
    "config_path": "<your-private-overlay>/config/podcast_ingest.yaml"
  }'
```

`config_path` supplies operator values (`indexer_scripts`, `manifest_path`) from
the private side. Or pass `indexer_scripts` directly. `"force": true` re-indexes
even if hash unchanged. Invoke by absolute path (hyphenated folder name).

## Why a thin wrapper

The operator's `index.py` owns embed + chunk + upsert + the ChromaDB collection.
This skill only scopes the scan to one directory and reuses that indexer — one
source of truth, no second codepath to drift.

## Composes with

- Upstream: `SAI-local-transcribe`
- Companion: a general-purpose `rag-index-updater` (full-tree variant)
- Downstream consumer: a `rag-content-lookup` (semantic search over results)
