"""Single-function query helper for skill cascade tier handlers.

Skills don't import chromadb directly — they call ``query()`` with
the operator-configured store + question. The function returns
``QueryResult[]`` (typed Pydantic; #6a).
"""

from __future__ import annotations

from app.rag.models import QueryResult
from app.rag.store import VectorStore


def query(
    *,
    store: VectorStore,
    question: str,
    n_results: int = 5,
    max_distance: float | None = None,
) -> list[QueryResult]:
    """Run a similarity query, optionally filtering low-quality matches.

    ``max_distance`` is a quality floor: hits with distance > this are
    dropped. Useful when the cascade should treat a noisy match the
    same as no match (escalate rather than draft on weak grounding).
    Default None = return everything the store gives back.
    """

    if not question.strip():
        return []
    raw = store.query(question, n_results=n_results)
    if max_distance is None:
        return raw
    return [r for r in raw if r.distance <= max_distance]
