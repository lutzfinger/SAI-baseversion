# podcast-rag-index

Atomic SAI skill. Indexes a directory of transcript Markdown files into an
existing ChromaDB RAG store by delegating to a `rag-index-updater`-style
`index.py`. Pure mechanism — it bakes in **no** operator paths or collection
name; the caller supplies those (directly or via a private `config_path`).

## What it does

1. `locate_indexer` — resolve the indexer scripts dir (the dir holding
   `index.py`) from the `indexer_scripts` input or the `config_path` YAML.
   Fail-closed if unresolved.
2. `scan_inputs` — walk `input_dir` for `.md` files, hash each, and (if a
   `manifest_path` is given) compare against it so only `new`/`changed` files
   survive. With no manifest, every `.md` is indexed.
3. `run_indexer` — shell out to `index.py --files <scan.json>`. That indexer
   owns the embedding model and the ChromaDB collection.

## Inputs

| input | required | meaning |
|---|---|---|
| `input_dir` | yes | directory of `.md` transcripts to index |
| `indexer_scripts` | yes* | dir containing the operator's `index.py` |
| `manifest_path` | no | manifest JSON for hash-diff resume |
| `config_path` | no | private YAML supplying defaults for the two above |
| `force` | no | re-index even if hash unchanged |

\* required unless supplied by `config_path`.

## CLI

```bash
.venv/bin/python3.12 \
  skills/podcast-rag-index/runner.py run \
  --inputs-json '{
    "input_dir":   "~/Media/transcripts/<show-slug>",
    "config_path": "<your-private-overlay>/config/podcast_ingest.yaml"
  }'
```

The `config_path` keeps operator values (index location, manifest path) on the
private side; this public skill only ships the loader, not the values.

## Why a thin wrapper

A `rag-index-updater`-style `index.py` already knows how to embed and upsert.
Re-implementing that here would create a second indexing codepath that could
drift. This wrapper just scopes the scan to one directory and reuses the proven
indexer.

## Composes with

- Upstream: `SAI-local-transcribe` (writes `.md` transcripts into the dir this
  skill consumes)
- Companion: a general-purpose `rag-index-updater` that scans a whole content
  tree
