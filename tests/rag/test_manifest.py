"""Tests for the RAG index manifest."""

from __future__ import annotations

from pathlib import Path

from app.rag.manifest import IndexManifest


def test_manifest_load_missing_file_returns_empty(tmp_path: Path):
    m = IndexManifest.load(tmp_path / "nope.json")
    assert m.entries == {}


def test_manifest_record_then_save_then_load_roundtrip(tmp_path: Path):
    m = IndexManifest()
    m.record(source_path="a.md", sha256="abc123")
    m.record(source_path="b.md", sha256="def456")
    p = tmp_path / "manifest.json"
    m.save(p)

    m2 = IndexManifest.load(p)
    assert set(m2.entries.keys()) == {"a.md", "b.md"}
    assert m2.entries["a.md"].sha256 == "abc123"


def test_manifest_is_unchanged_returns_true_when_match(tmp_path: Path):
    m = IndexManifest()
    m.record(source_path="x.md", sha256="hash1")
    assert m.is_unchanged(source_path="x.md", sha256="hash1") is True
    assert m.is_unchanged(source_path="x.md", sha256="hash2") is False
    assert m.is_unchanged(source_path="y.md", sha256="hash1") is False


def test_manifest_remove(tmp_path: Path):
    m = IndexManifest()
    m.record(source_path="x.md", sha256="hash1")
    m.remove("x.md")
    assert "x.md" not in m.entries
    # Removing a missing entry is a no-op
    m.remove("never-existed.md")


def test_manifest_load_legacy_hash_field(tmp_path: Path):
    """The operator's existing manifest.json uses `hash` not `sha256`.
    Loader should accept both."""
    p = tmp_path / "manifest.json"
    p.write_text(
        '{"old.md": {"hash": "legacy-sha", "indexed_at": "2026-01-01"}}',
        encoding="utf-8",
    )
    m = IndexManifest.load(p)
    assert m.entries["old.md"].sha256 == "legacy-sha"
