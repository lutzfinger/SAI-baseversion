"""Tests for the RAG document loader + chunker."""

from __future__ import annotations

from pathlib import Path

from app.rag.loader import chunk_text, load_documents


def test_load_documents_skips_unsupported_extensions(tmp_path: Path):
    (tmp_path / "a.md").write_text("# Hello\n\nWorld.\n", encoding="utf-8")
    (tmp_path / "b.png").write_text("not really a png", encoding="utf-8")
    (tmp_path / "c.txt").write_text("Plain text.", encoding="utf-8")
    docs = list(load_documents(content_root=tmp_path))
    sources = {d.source_path for d in docs}
    assert sources == {"a.md", "c.txt"}


def test_load_documents_chunks_long_files(tmp_path: Path):
    para = "This is paragraph " + "x" * 800 + "."
    (tmp_path / "long.md").write_text(
        f"{para}\n\n{para}\n\n{para}\n\n{para}\n",
        encoding="utf-8",
    )
    docs = list(load_documents(
        content_root=tmp_path, max_chunk_chars=1500,
    ))
    assert len(docs) >= 2  # 4 paragraphs of ~800 chars → multiple chunks


def test_load_documents_skips_dotfiles_by_default(tmp_path: Path):
    (tmp_path / ".hidden.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "visible.md").write_text("visible", encoding="utf-8")
    docs = list(load_documents(content_root=tmp_path))
    assert {d.source_path for d in docs} == {"visible.md"}


def test_load_documents_skips_empty_files(tmp_path: Path):
    (tmp_path / "empty.md").write_text("   \n\n  ", encoding="utf-8")
    (tmp_path / "real.md").write_text("real content", encoding="utf-8")
    docs = list(load_documents(content_root=tmp_path))
    assert {d.source_path for d in docs} == {"real.md"}


def test_load_documents_assigns_chunk_indices(tmp_path: Path):
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    (tmp_path / "a.md").write_text(text, encoding="utf-8")
    docs = list(load_documents(content_root=tmp_path, max_chunk_chars=20))
    indices = [d.chunk_index for d in docs]
    assert indices == sorted(indices)
    assert indices[0] == 0


def test_chunk_text_preserves_short_input():
    chunks = list(chunk_text("Short text.", max_chars=1500))
    assert chunks == ["Short text."]


def test_chunk_text_splits_at_paragraph_boundary():
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    chunks = list(chunk_text(text, max_chars=20))
    # Each paragraph is small enough to fit in 20 chars on its own,
    # so each gets its own chunk
    assert len(chunks) == 3


def test_chunk_text_handles_oversized_paragraph():
    long = "x" * 5000
    chunks = list(chunk_text(long, max_chars=1000))
    assert all(len(c) <= 1000 for c in chunks)
    assert len(chunks) == 5  # 5000 / 1000


def test_load_documents_raises_on_missing_root(tmp_path: Path):
    import pytest
    with pytest.raises(FileNotFoundError):
        list(load_documents(content_root=tmp_path / "nope"))
