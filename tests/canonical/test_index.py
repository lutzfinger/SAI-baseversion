"""Tests for the canonical index helper."""

from __future__ import annotations

from dataclasses import dataclass

from app.canonical.index import build_index


@dataclass
class _Item:
    name: str
    aliases: list[str]


def test_build_index_one_item_one_key():
    idx = build_index(
        items=[_Item("a", ["x"])], keys_for=lambda i: i.aliases,
    )
    out = idx.lookup("x")
    assert len(out) == 1
    assert out[0].name == "a"


def test_build_index_one_item_many_keys():
    idx = build_index(
        items=[_Item("a", ["x", "y", "z"])], keys_for=lambda i: i.aliases,
    )
    assert len(idx.lookup("x")) == 1
    assert len(idx.lookup("y")) == 1
    assert len(idx.lookup("z")) == 1


def test_build_index_many_items_shared_key():
    idx = build_index(
        items=[_Item("a", ["shared"]), _Item("b", ["shared"])],
        keys_for=lambda i: i.aliases,
    )
    out = idx.lookup("shared")
    names = sorted(i.name for i in out)
    assert names == ["a", "b"]


def test_lookup_missing_returns_empty():
    idx = build_index(items=[_Item("a", ["x"])], keys_for=lambda i: i.aliases)
    assert idx.lookup("nope") == []


def test_lookup_any_dedupes():
    item = _Item("a", ["x", "y"])
    idx = build_index(items=[item], keys_for=lambda i: i.aliases)
    out = idx.lookup_any(["x", "y", "z"])
    # Same item once — even though both 'x' and 'y' would match.
    assert len(out) == 1


def test_lookup_any_preserves_order_first_seen():
    a = _Item("a", ["k1"])
    b = _Item("b", ["k2"])
    idx = build_index(items=[a, b], keys_for=lambda i: i.aliases)
    out = idx.lookup_any(["k2", "k1"])
    # First-seen via "k2" → b first, then a.
    assert [i.name for i in out] == ["b", "a"]


def test_size_counts_unique_items():
    a = _Item("a", ["k1", "k2"])
    b = _Item("b", ["k1"])
    idx = build_index(items=[a, b], keys_for=lambda i: i.aliases)
    assert idx.size() == 2


def test_keys_lists_all_indexed_keys():
    idx = build_index(
        items=[_Item("a", ["x", "y"]), _Item("b", ["y", "z"])],
        keys_for=lambda i: i.aliases,
    )
    keys = sorted(idx.keys())
    assert keys == ["x", "y", "z"]


def test_empty_input_yields_empty_index():
    idx = build_index(items=[], keys_for=lambda i: [])
    assert idx.size() == 0
    assert idx.keys() == []
    assert idx.lookup("anything") == []
