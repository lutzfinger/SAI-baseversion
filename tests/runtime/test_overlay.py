"""Tests for app.runtime.overlay — public/private merge tooling."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from app.runtime.overlay import (
    MANIFEST_FILENAME,
    SCHEMA_VERSION,
    InputError,
    TypeConflictError,
    merge,
)

DEMO_FIXTURES = Path(__file__).parent / "fixtures" / "demo"


TreeBuilder = Callable[[str, dict[str, str]], Path]


@pytest.fixture
def make_tree(tmp_path: Path) -> TreeBuilder:
    """Build a tree under tmp_path from a {relpath: content} dict."""

    def _make(name: str, layout: dict[str, str]) -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        for relpath, content in layout.items():
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root

    return _make


@pytest.fixture
def out_path(tmp_path: Path) -> Path:
    return tmp_path / "out"


# 1
def test_empty_private_yields_public_only(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"a/x.yaml": "x", "b/y.yaml": "y"})
    private = make_tree("prv", {})
    r = merge(public=public, private=private, out=out_path)
    assert r.shadowed_count == 0
    assert r.file_count == 2
    assert (out_path / "a/x.yaml").read_text() == "x"
    assert all(e.source == "public" for e in r.files.values())


# 2
def test_private_overrides_public(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"workflows/x.yaml": "from-public"})
    private = make_tree("prv", {"workflows/x.yaml": "from-private"})
    r = merge(public=public, private=private, out=out_path)
    assert (out_path / "workflows/x.yaml").read_text() == "from-private"
    assert r.files["workflows/x.yaml"].source == "private"


# 3
def test_shadowed_count_matches_overrides(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree(
        "pub",
        {"a.yaml": "1", "b.yaml": "2", "c.yaml": "3", "d.yaml": "4"},
    )
    private = make_tree(
        "prv",
        {"a.yaml": "1p", "c.yaml": "3p", "e.yaml": "5p"},
    )
    r = merge(public=public, private=private, out=out_path)
    assert r.shadowed_count == 2
    assert set(r.shadowed_files) == {"a.yaml", "c.yaml"}


# 4
def test_shadowed_files_list_is_sorted(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"z.yaml": "z", "a.yaml": "a", "m.yaml": "m"})
    private = make_tree("prv", {"z.yaml": "z2", "a.yaml": "a2", "m.yaml": "m2"})
    r = merge(public=public, private=private, out=out_path)
    assert r.shadowed_files == ["a.yaml", "m.yaml", "z.yaml"]


# 5
def test_private_only_files_added(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {"new/only.yaml": "private-only"})
    r = merge(public=public, private=private, out=out_path)
    assert (out_path / "new/only.yaml").read_text() == "private-only"
    assert r.files["new/only.yaml"].source == "private"
    assert r.shadowed_count == 0


# 6
def test_manifest_hash_matches_file_content(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    body = "name: hello\nversion: 1\n"
    public = make_tree("pub", {"workflows/x.yaml": body})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    manifest = json.loads((out_path / MANIFEST_FILENAME).read_text())
    expected = hashlib.sha256(body.encode()).hexdigest()
    assert manifest["files"]["workflows/x.yaml"]["sha256"] == expected


# 7
def test_manifest_schema_required_fields(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    manifest = json.loads((out_path / MANIFEST_FILENAME).read_text())
    for field in (
        "schema_version",
        "mode",
        "created_at",
        "public_root",
        "private_root",
        "shadowed_count",
        "shadowed_files",
        "files",
    ):
        assert field in manifest, f"manifest missing field {field!r}"
    assert manifest["schema_version"] == SCHEMA_VERSION


# 8
def test_manifest_mode_copy_default(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    manifest = json.loads((out_path / MANIFEST_FILENAME).read_text())
    assert manifest["mode"] == "copy"


# 9
def test_manifest_mode_symlink_when_requested(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path, mode="symlink")
    manifest = json.loads((out_path / MANIFEST_FILENAME).read_text())
    assert manifest["mode"] == "symlink"


# 10
def test_symlink_mode_produces_symlinks(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {"b.yaml": "b"})
    merge(public=public, private=private, out=out_path, mode="symlink")
    assert (out_path / "a.yaml").is_symlink()
    assert (out_path / "b.yaml").is_symlink()
    # Manifest itself is a regular file, not a symlink.
    assert not (out_path / MANIFEST_FILENAME).is_symlink()


# 11
def test_empty_file_handled(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"empty.yaml": ""})
    private = make_tree("prv", {})
    r = merge(public=public, private=private, out=out_path)
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert r.files["empty.yaml"].sha256 == empty_hash
    assert r.files["empty.yaml"].size_bytes == 0


# 12
def test_nonexistent_public_errors(tmp_path: Path, out_path: Path) -> None:
    private = tmp_path / "prv"
    private.mkdir()
    with pytest.raises(InputError, match="--public"):
        merge(public=tmp_path / "missing", private=private, out=out_path)


# 13
def test_nonexistent_private_errors(tmp_path: Path, out_path: Path) -> None:
    public = tmp_path / "pub"
    public.mkdir()
    with pytest.raises(InputError, match="--private"):
        merge(public=public, private=tmp_path / "missing", out=out_path)


# 14
def test_existing_out_without_clean_errors(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    out_path.mkdir()
    (out_path / "stale.txt").write_text("stale")
    with pytest.raises(InputError, match="already exists"):
        merge(public=public, private=private, out=out_path)


# 15
def test_existing_out_with_clean_succeeds(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    out_path.mkdir()
    (out_path / "stale.txt").write_text("stale")
    r = merge(public=public, private=private, out=out_path, clean=True)
    assert not (out_path / "stale.txt").exists()
    assert r.file_count == 1


# 16
def test_out_inside_public_errors(make_tree: TreeBuilder) -> None:
    public = make_tree("pub", {"a.yaml": "a"})
    private = make_tree("prv", {})
    bad_out = public / "subdir"
    with pytest.raises(InputError, match="cannot be inside"):
        merge(public=public, private=private, out=bad_out)


# 17
def test_git_dir_skipped(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree(
        "pub",
        {
            "a.yaml": "a",
            ".git/HEAD": "ref: refs/heads/main",
            ".git/config": "[core]",
        },
    )
    private = make_tree("prv", {".git/objects/abc": "blob"})
    r = merge(public=public, private=private, out=out_path)
    assert "a.yaml" in r.files
    assert not any(rel.startswith(".git/") for rel in r.files)
    assert not (out_path / ".git").exists()


# 18
def test_pycache_and_pyc_skipped(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree(
        "pub",
        {
            "module.py": "x = 1",
            "__pycache__/module.cpython-312.pyc": "BYTECODE",
            "stray.pyc": "BYTECODE",
        },
    )
    private = make_tree("prv", {})
    r = merge(public=public, private=private, out=out_path)
    assert "module.py" in r.files
    assert not any(
        "__pycache__" in rel or rel.endswith(".pyc") for rel in r.files
    )


# 19
def test_ds_store_skipped(make_tree: TreeBuilder, out_path: Path) -> None:
    public = make_tree("pub", {"a.yaml": "a", ".DS_Store": "macOS junk"})
    private = make_tree("prv", {".DS_Store": "more junk"})
    r = merge(public=public, private=private, out=out_path)
    assert ".DS_Store" not in r.files
    assert not (out_path / ".DS_Store").exists()


# 20
def test_nested_directories_preserved(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree(
        "pub", {"a/b/c/deep.yaml": "deep", "a/sibling.yaml": "sibling"}
    )
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    assert (out_path / "a/b/c/deep.yaml").read_text() == "deep"
    assert (out_path / "a/sibling.yaml").read_text() == "sibling"


# 21
def test_type_conflict_errors_dir_over_file(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    public = make_tree("pub", {"x/inner.yaml": "i"})
    private = make_tree("prv", {"x": "now-a-file"})
    with pytest.raises(TypeConflictError, match="type conflict"):
        merge(public=public, private=private, out=out_path)


# 22
def test_demo_fixtures_yield_shadowed_count_one(out_path: Path) -> None:
    """Ties the manual demo in HANDOFF.md to a real assertion."""
    r = merge(
        public=DEMO_FIXTURES / "public",
        private=DEMO_FIXTURES / "private",
        out=out_path,
    )
    assert r.shadowed_count == 1
    assert r.shadowed_files == ["workflows/_examples/hello.yaml"]
    # Private overrode the public hello; private-only extra was added;
    # public-only other and policies/_examples/hello survived.
    assert (
        out_path / "workflows/_examples/hello.yaml"
    ).read_text().splitlines()[0] == "name: hello-world"
    assert "workflows/_examples/extra.yaml" in r.files
    assert "workflows/_examples/other.yaml" in r.files
    assert "policies/_examples/hello.yaml" in r.files
    assert r.files["workflows/_examples/hello.yaml"].source == "private"


# Smoke test that the runtime is round-trippable: merge then re-hash.
def test_verify_roundtrip_clean(make_tree: TreeBuilder, out_path: Path) -> None:
    from app.runtime.overlay import verify

    public = make_tree("pub", {"a.yaml": "a", "b/c.yaml": "c"})
    private = make_tree("prv", {"a.yaml": "a-private"})
    merge(public=public, private=private, out=out_path)
    mismatches, missing, unregistered = verify(out_path)
    assert mismatches == [] and missing == [] and unregistered == []


def test_verify_detects_tampering(make_tree: TreeBuilder, out_path: Path) -> None:
    from app.runtime.overlay import verify

    public = make_tree("pub", {"a.yaml": "original"})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    (out_path / "a.yaml").write_text("tampered")
    mismatches, _missing, _unreg = verify(out_path)
    assert mismatches == ["a.yaml"]


def test_verify_detects_unregistered_and_missing(
    make_tree: TreeBuilder, out_path: Path
) -> None:
    from app.runtime.overlay import verify

    public = make_tree("pub", {"a.yaml": "a", "b.yaml": "b"})
    private = make_tree("prv", {})
    merge(public=public, private=private, out=out_path)
    # Add a stray file (unregistered) and remove a manifest file (missing).
    (out_path / "stray.yaml").write_text("not in manifest")
    os.remove(out_path / "b.yaml")
    mismatches, missing, unregistered = verify(out_path)
    assert mismatches == []
    assert missing == ["b.yaml"]
    assert unregistered == ["stray.yaml"]
