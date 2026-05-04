"""Inverted-index helper for canonical loaders (#33a — framework
primitive, not skill).

Canonical lookups today are O(N × M). For small registries
(courses, TAs) the cost is invisible; once an operator has 50+
courses or hundreds of TAs the linear scan starts to matter. This
module turns any canonical loader into an O(1)-by-token lookup
without changing the loader's public API.

Pattern:

    @lru_cache(maxsize=1)
    def courses_by_identifier() -> KeyIndex[Course]:
        return build_index(
            items=courses.all_courses().values(),
            keys_for=lambda c: [i.lower() for i in c.identifiers],
        )

    matches = courses_by_identifier().lookup(token.lower())

Mechanism in framework. The skill never imports this module — it
calls the loader's existing high-level helpers
(`infer_course_from_text`, `get_active_tas_for_course`); those
helpers can be rewritten to use the index when scale demands it
WITHOUT a skill-side change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")


@dataclass
class KeyIndex(Generic[T]):
    """Inverted index: token → list[item_with_that_token]."""

    by_key: dict[str, list[T]] = field(default_factory=lambda: defaultdict(list))

    def lookup(self, key: str) -> list[T]:
        """O(1) — return all items registered under `key`. Empty
        list if no match."""
        return list(self.by_key.get(key, []))

    def lookup_any(self, keys: Iterable[str]) -> list[T]:
        """Union of `lookup(k) for k in keys`, deduped (preserves
        first-seen order). Useful when the caller has multiple
        candidate tokens (e.g. several substrings)."""
        seen_ids: set[int] = set()
        out: list[T] = []
        for key in keys:
            for item in self.by_key.get(key, []):
                if id(item) in seen_ids:
                    continue
                seen_ids.add(id(item))
                out.append(item)
        return out

    def keys(self) -> list[str]:
        return list(self.by_key.keys())

    def size(self) -> int:
        """Number of unique items in the index (not entries — items
        with multiple keys count once)."""
        seen: set[int] = set()
        for items in self.by_key.values():
            for item in items:
                seen.add(id(item))
        return len(seen)


def build_index(
    *,
    items: Iterable[T],
    keys_for: Callable[[T], Iterable[str]],
) -> KeyIndex[T]:
    """Build a KeyIndex from an iterable of items + a key-extractor.

    Each item may register under multiple keys (e.g. a course with
    multiple identifiers); a single key may map to multiple items
    (e.g. a domain shared by two senders).

    Caller is responsible for case-folding keys before passing them
    in — the index does not normalize.
    """

    out: KeyIndex[T] = KeyIndex()
    for item in items:
        for key in keys_for(item):
            out.by_key[key].append(item)
    return out
