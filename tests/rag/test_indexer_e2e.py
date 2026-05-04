"""End-to-end test: indexer + ChromaVectorStore round-trip.

Uses chromadb's PersistentClient against a tmp_path. No network. The
embedding function (sentence-transformers all-MiniLM-L6-v2) needs to
be available — chromadb auto-downloads it if missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Allow the test suite to skip RAG e2e on environments without
# chromadb installed (e.g. minimal CI runners).
chromadb = pytest.importorskip("chromadb")

from app.rag import ChromaVectorStore, build_or_update_index, query


@pytest.fixture
def tmp_index(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (content_root, persist_path, manifest_path)."""
    content_root = tmp_path / "content"
    content_root.mkdir()
    persist_path = tmp_path / "chroma"
    manifest_path = tmp_path / "manifest.json"
    return content_root, persist_path, manifest_path


def test_full_index_then_query_roundtrip(tmp_index: tuple[Path, Path, Path]):
    content_root, persist_path, manifest_path = tmp_index
    (content_root / "a.md").write_text(
        "Cats are small carnivorous mammals known for their independence.",
        encoding="utf-8",
    )
    (content_root / "b.md").write_text(
        "Python is a high-level programming language with dynamic typing.",
        encoding="utf-8",
    )
    store = ChromaVectorStore(
        persist_path=persist_path, collection_name="test_collection",
    )

    result = build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )

    assert result.files_added == 2
    assert result.chunks_upserted == 2
    assert store.count() == 2

    # Query for cat-related → should hit a.md
    hits = query(store=store, question="feline pets", n_results=1)
    assert len(hits) == 1
    assert hits[0].document.source_path == "a.md"


def test_reindex_unchanged_files_is_a_noop(tmp_index: tuple[Path, Path, Path]):
    content_root, persist_path, manifest_path = tmp_index
    (content_root / "a.md").write_text("Stable content.", encoding="utf-8")
    store = ChromaVectorStore(
        persist_path=persist_path, collection_name="test_noop",
    )
    build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    # Second run: nothing should change
    result = build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    assert result.files_added == 0
    assert result.files_changed == 0
    assert result.files_skipped_unchanged == 1
    assert result.chunks_upserted == 0


def test_reindex_after_file_change_replaces_chunks(tmp_index: tuple[Path, Path, Path]):
    content_root, persist_path, manifest_path = tmp_index
    f = content_root / "a.md"
    f.write_text("Original content about cats.", encoding="utf-8")
    store = ChromaVectorStore(
        persist_path=persist_path, collection_name="test_change",
    )
    build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    # Edit the file
    f.write_text("Updated content about dogs.", encoding="utf-8")
    result = build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    assert result.files_changed == 1
    # Query should now match dogs, not cats
    hits = query(store=store, question="canine companions", n_results=1)
    assert len(hits) == 1
    assert "dogs" in hits[0].document.text.lower()


def test_reindex_after_file_removal_drops_chunks(tmp_index: tuple[Path, Path, Path]):
    content_root, persist_path, manifest_path = tmp_index
    a = content_root / "a.md"
    b = content_root / "b.md"
    a.write_text("Keep me.", encoding="utf-8")
    b.write_text("Delete me later.", encoding="utf-8")
    store = ChromaVectorStore(
        persist_path=persist_path, collection_name="test_remove",
    )
    build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    assert store.count() == 2

    b.unlink()
    result = build_or_update_index(
        content_root=content_root, store=store, manifest_path=manifest_path,
    )
    assert result.files_removed == 1
    assert store.count() == 1


def test_query_with_max_distance_filter():
    """Bare in-process Chroma — verifies the max_distance gate."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = ChromaVectorStore(
            persist_path=Path(tmp), collection_name="test_distance",
        )
        # Manually upsert a doc
        from app.rag.models import Document
        store.upsert([Document(
            source_path="x.md", chunk_index=0,
            text="Quantum chromodynamics describes the strong force.",
            source_sha256="x",
        )])
        # Off-topic query should return the doc but with high distance
        hits = query(store=store, question="recipes for chocolate cake")
        assert len(hits) == 1
        # Filter with very low max_distance → drops the off-topic hit
        hits_filtered = query(
            store=store, question="recipes for chocolate cake",
            max_distance=0.1,
        )
        assert hits_filtered == []
