# Design: RAG framework primitive (`app/rag/`)

**STATUS:** SHIPPED 2026-05-04. Per PRINCIPLES.md §33a — primitives
ship separately from skills; this document is the primitive's design
record. Skills that USE RAG (e.g. an "ask my writing" agent) get
designed in Co-Work and reference this primitive.

**Maps to:** PRINCIPLES.md §13 (pluggable Provider abstraction),
§14 (factories everywhere), §16f (agent execution planes), §17
(public mechanism / private values), §6a (every input + output
guarded), §33a (skills compose primitives — RAG is a primitive).

---

## Why this is a framework primitive, not a skill

Per §33a a skill is a declarative composition of EXISTING primitives.
RAG retrieval — a vector store, an embedding pipeline, a chunker, an
indexer — is infrastructure other skills will need to reuse. If the
first skill that needs RAG inlines the chromadb calls, the second
skill that needs RAG ends up duplicating it (different chunker,
different distance threshold, different metadata schema), and the
audit log + cost-tracking can't roll up across them.

The skill-creator prompt (`docs/cowork_skill_creator_prompt.md`)
explicitly listed RAG as a *forbidden inline addition* until this
primitive shipped. Now that it's shipped, skills can compose it.

## Public surface

```
app/rag/__init__.py       — re-exports the public symbols
app/rag/models.py         — Document + QueryResult (Pydantic, extra=forbid)
app/rag/store.py          — VectorStore Protocol (vendor-portable)
app/rag/chroma_store.py   — ChromaVectorStore (concrete impl)
app/rag/loader.py         — load_documents + chunk_text
app/rag/indexer.py        — build_or_update_index (incremental)
app/rag/manifest.py       — IndexManifest (sha256 → indexed_at, on disk)
app/rag/query.py          — query() — single function for tier handlers

app/agents/rag_tools.py   — query_rag + list_rag_collections agent tools
                            for the #sai-rag channel

scripts/sai_rag_index.py  — CLI: build / update an index
```

`config/channel_allowed_discussion.yaml` gains a `sai-rag` channel
with the `rag_query` topic (risk_class=low, tools=[query_rag,
list_rag_collections]).

## Architecture decisions

### 1. Protocol-first, vendor-second (#13)

`VectorStore` is a `Protocol`; `ChromaVectorStore` is one
implementation. Skills depend on the protocol. Future implementations
(pinecone, weaviate, qdrant, in-memory for tests) are drop-in
replacements: `_build_query_rag(ctx)` only knows about `VectorStore`.

### 2. Chroma defaults match operator's existing index

The operator already has `~/Lutz_Media/rag-index/chroma/` — collection
`lutz_author`, dimension 384, ~4400 documents — built with chromadb's
default embedding function (`all-MiniLM-L6-v2` via sentence-transformers
auto-downloaded as ONNX). We use the SAME default so the existing
index keeps working without re-indexing. Pinning a different model
would force a full rebuild.

### 3. Manifest is the source of truth for "indexed when"

`<index_root>/manifest.json` records `{relative_path: {sha256, indexed_at}}`.
The indexer is incremental: on the second run, only files whose
sha256 changed get re-upserted. New files get added; deleted files
get pruned. **Manifest format intentionally matches the operator's
existing manifest.json** (legacy `hash` field accepted by the loader)
so re-indexing the existing corpus is also a no-op until files
change.

### 4. Document path is RELATIVE to content_root

Stored doc_ids look like `2026-04-25-meta-laid-off.md::chunk::3`
— no `~/`, no `/Users/`, no machine-specific paths. Index portable
across machines / containers / Cowork sessions. The operator's
existing index has session-sandbox absolute paths (legacy from a
Cowork session); future re-indexing produces clean relative paths.

### 5. Chunker: paragraph-first with a hard char cap

Chunking by `\n\n` preserves semantic units (paragraphs, list items,
headings) which embedding models handle better than fixed-token
windows. Long paragraphs split at sentence boundaries; oversized
sentences split at character boundaries (last resort). Default
`max_chunk_chars=1500` — fits comfortably in MiniLM's token budget,
gives 4-5 hits in a typical N=5 query.

Tradeoff: paragraph-first means short paragraphs get their own
chunk (slightly more index rows than a fixed-window chunker would
produce), but query relevance is better. We optimize for relevance,
not index size.

### 6. Optional dependency

`chromadb` is in `[project.optional-dependencies] rag = [chromadb]`.
Strangers cloning the repo who don't want RAG don't pay the
~80MB transitive cost. Skills that NEED RAG fail at import time
with a clear "install with `pip install -e .[rag]`" message.

### 7. Per-channel agent tools (sai-rag)

Per #16f the agent execution plane has bounded tools. RAG agent
gets two:
- `query_rag(collection, question, n_results, max_distance)` —
  similarity search, returns top-N passages with source paths.
- `list_rag_collections()` — read-only listing of which collections
  the agent can query.

Both `read_only`. No proposals, no side effects. The retrieval
SURFACE is read-only; the operator's question + the agent's distilled
reply go to `#sai-rag` (the channel's existence is the access-control
boundary — risk_class `low` because content is operator-private).

## What this primitive does NOT do

- **No re-ranking.** First-pass cosine similarity. Skills that need
  re-ranking can layer it on top via a second-tier (LLM-based
  re-rank, or a future `app/rag/rerank.py` primitive).
- **No prompt-stuffing for synthesis.** The query function returns
  passages; the SKILL composes them into an LLM prompt and asks the
  LLM to synthesize. (That's a skill design choice — citation
  format, refusal-when-grounding-weak threshold, etc. — and belongs
  in the skill, not the primitive.)
- **No multi-collection federation.** One query → one collection.
  Skills that need to search across multiple collections call query
  N times (or a future `multi_query` primitive lands when a second
  skill needs it).
- **No document loaders for PDF / DOCX / HTML.** Markdown + text
  only. Adding a PDF loader is straightforward (`pypdf` is already a
  dep) but should land when a skill actually needs it — not before.
- **No incremental embeddings cache.** Chromadb stores embeddings
  with the docs; re-running on unchanged files skips embedding
  altogether. If embeddings ever come from a paid API (Voyage,
  OpenAI), this primitive will need to track per-file embedding cost
  in the audit log — not built today.

## Operator-side wiring

Today (manual, until Co-Work designs the SAI RAG skill):

```sh
# Index the operator's content
.venv/bin/python -m scripts.sai_rag_index \
    --content-root ~/Lutz_Media/Lutz-author \
    --persist-path ~/Lutz_Media/rag-index/chroma \
    --collection lutz_author
```

Tomorrow (after Co-Work designs the sai-rag skill): the skill's
runner constructs a `RagToolContext` populated from operator config
(maps collection-id → ChromaVectorStore for the operator's
`lutz_author` collection), and the agent runner exposes
`query_rag` + `list_rag_collections` to the LLM on `#sai-rag`.

## Eval contract

The primitive's tests live at `tests/rag/`:
- `test_loader.py` — chunker boundary cases (paragraph, sentence,
  oversized-paragraph, dotfiles, empty files)
- `test_manifest.py` — round-trip + legacy `hash`-field tolerance +
  is_unchanged semantics
- `test_indexer_e2e.py` — full chroma round-trip (add / change /
  remove / no-op re-run / max_distance filter)
- `test_agent_tools.py` — query_rag + list_rag_collections + input
  validation

24 tests at ship; auto-skip on environments without chromadb
installed (per `pytest.importorskip`).

When a skill USES the RAG primitive, that skill's
`workflow_regression.jsonl` exercises end-to-end behavior (operator
question → expected source citations → expected refusal-when-
grounding-weak). Per #16d every skill ships its own eval contract.

## Migration & rollout

1. **2026-05-04 (this commit):** primitive ships; operator's existing
   index restored from backup; CLI reindexes it incrementally on
   demand.
2. **Next: Co-Work designs the sai-rag SKILL** — runner config,
   collection→VectorStore mapping, system prompt for the agent (when
   to query, how to cite, how to refuse on weak grounding). Skill
   lands in `$SAI_PRIVATE/skills/sai-rag/`.
3. **Operator flips the channel ON:** add `#sai-rag` to the
   operator's Slack workspace; the bot picks up the topic from
   `channel_allowed_discussion.yaml` and starts handling questions.
4. **Eventually:** generalize `chunk_text` to a `Chunker` Protocol;
   add a `PdfLoader`; add re-rank as a secondary tier.

## Risks + mitigations

- **chromadb auto-downloads ~80MB ONNX model on first run.** Operator
  on a slow network sees a one-time delay. Mitigation: documented in
  the design + indexer prints progress.
- **Embedding model differs from chromadb's default → existing index
  becomes garbage.** Mitigation: ChromaVectorStore uses chromadb's
  default unless an `embedding_function` is explicitly passed. Don't
  pass one for the operator's existing index.
- **No grounding threshold by default → agent might cite weak
  matches.** Mitigation: skill designers set `max_distance` per
  collection; default behavior surfaces all hits but the SKILL gates
  what reaches the operator.
- **No PII redaction in returned chunks.** RAG returns whatever's in
  the index. If the corpus has PII, it surfaces in #sai-rag replies.
  Mitigation: operator chooses what to index; the channel itself is
  the access-control boundary (risk_class=low, not minimal).
