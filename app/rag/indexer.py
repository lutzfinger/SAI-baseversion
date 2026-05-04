"""Index builder — incremental indexing driven by the manifest.

Three operations per run:
  1. New files (in content_root, not in manifest) → upsert + record
  2. Changed files (sha256 differs from manifest) → upsert + update record
  3. Removed files (in manifest, no longer on disk) → delete chunks + drop record

The manifest is the source of truth for "what's been indexed" — the
vector store is the source of truth for "what's queryable." The
indexer reconciles them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.rag.loader import DEFAULT_EXTENSIONS, DEFAULT_MAX_CHUNK_CHARS, load_documents
from app.rag.manifest import IndexManifest
from app.rag.store import VectorStore

LOGGER = logging.getLogger(__name__)


@dataclass
class IndexUpdateResult:
    files_added: int = 0
    files_changed: int = 0
    files_removed: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0
    files_skipped_unchanged: int = 0


def build_or_update_index(
    *,
    content_root: Path,
    store: VectorStore,
    manifest_path: Path,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> IndexUpdateResult:
    """Reconcile ``store`` against the files under ``content_root``.

    Idempotent: running twice with no file changes is a no-op (chunks
    skipped because manifest sha256 matches).
    """

    if not content_root.is_dir():
        raise NotADirectoryError(f"content_root is not a directory: {content_root}")

    manifest = IndexManifest.load(manifest_path)
    result = IndexUpdateResult()

    # Group documents by source so we can decide per-file whether to
    # skip or upsert.
    by_source: dict[str, list] = defaultdict(list)
    sha_by_source: dict[str, str] = {}
    for doc in load_documents(
        content_root=content_root,
        extensions=extensions,
        max_chunk_chars=max_chunk_chars,
    ):
        by_source[doc.source_path].append(doc)
        sha_by_source[doc.source_path] = doc.source_sha256

    seen_sources = set(by_source.keys())
    manifest_sources = set(manifest.entries.keys())

    # Process additions + changes
    to_upsert = []
    for source_path, docs in by_source.items():
        sha = sha_by_source[source_path]
        if manifest.is_unchanged(source_path=source_path, sha256=sha):
            result.files_skipped_unchanged += 1
            continue
        if source_path in manifest_sources:
            # Changed: drop old chunks before re-upserting (chunk count
            # may have changed → orphaned ids would linger)
            removed = store.delete_by_source([source_path])
            result.chunks_deleted += removed
            result.files_changed += 1
        else:
            result.files_added += 1
        to_upsert.extend(docs)
        manifest.record(source_path=source_path, sha256=sha)

    if to_upsert:
        result.chunks_upserted = store.upsert(to_upsert)

    # Process removals (files in manifest but no longer on disk)
    removed_sources = manifest_sources - seen_sources
    if removed_sources:
        removed_chunks = store.delete_by_source(removed_sources)
        result.chunks_deleted += removed_chunks
        for s in removed_sources:
            manifest.remove(s)
        result.files_removed = len(removed_sources)

    manifest.save(manifest_path)
    LOGGER.info(
        "indexed %s: +%d -%d ~%d (skipped=%d, +chunks=%d, -chunks=%d)",
        content_root,
        result.files_added,
        result.files_removed,
        result.files_changed,
        result.files_skipped_unchanged,
        result.chunks_upserted,
        result.chunks_deleted,
    )
    return result
