"""VectorStore Protocol — vendor-portable abstraction (#13 + #24a).

Skills depend on the protocol, not on chromadb / pinecone / weaviate
directly. Operators swap backends via configuration; skill code stays
unchanged.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from app.rag.models import Document, QueryResult


@runtime_checkable
class VectorStore(Protocol):
    """Minimal vector-store interface SAI skills depend on.

    Concrete implementations live in their own module
    (``chroma_store.py``, future: ``pinecone_store.py``, etc.).
    """

    @property
    def collection_name(self) -> str:
        """The collection / namespace this store writes to."""
        ...

    def upsert(self, documents: Iterable[Document]) -> int:
        """Insert or replace documents. Returns count written."""
        ...

    def delete(self, doc_ids: Iterable[str]) -> int:
        """Remove documents by id. Returns count removed."""
        ...

    def delete_by_source(self, source_paths: Iterable[str]) -> int:
        """Remove every chunk associated with the given source paths."""
        ...

    def query(self, query_text: str, *, n_results: int = 5) -> list[QueryResult]:
        """Top-N similarity search."""
        ...

    def count(self) -> int:
        """Total number of documents in the collection."""
        ...

    def all_doc_ids(self) -> set[str]:
        """Every doc_id currently in the collection (for reconciliation)."""
        ...
