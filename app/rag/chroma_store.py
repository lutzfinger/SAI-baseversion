"""ChromaDB-backed VectorStore implementation.

chromadb is an optional dependency (``pip install -e .[rag]``).
Workflows that don't use RAG don't need it. Workflows that DO use
RAG fail loudly at import time when it's missing — better than a
silent abstain at query time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from app.rag.models import Document, QueryResult


class ChromaVectorStore:
    """Persistent Chroma collection wrapped in the VectorStore protocol.

    Defaults to chromadb's built-in embedding function
    (``all-MiniLM-L6-v2``, 384-dim, sentence-transformers under the
    hood). Operators with an existing Chroma index built with the
    default keep working without re-indexing.
    """

    def __init__(
        self,
        *,
        persist_path: Path,
        collection_name: str,
        embedding_function: Optional[Any] = None,
    ) -> None:
        try:
            import chromadb
        except ImportError as exc:
            raise ImportError(
                "ChromaVectorStore requires the chromadb package. "
                "Install with: pip install -e '.[rag]'"
            ) from exc

        self._persist_path = persist_path
        self._collection_name = collection_name
        self._client = chromadb.PersistentClient(path=str(persist_path))
        kwargs: dict[str, Any] = {"name": collection_name}
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function
        self._collection = self._client.get_or_create_collection(**kwargs)

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def upsert(self, documents: Iterable[Document]) -> int:
        docs = list(documents)
        if not docs:
            return 0
        ids = [d.doc_id for d in docs]
        texts = [d.text for d in docs]
        metadatas = [
            {
                "source_path": d.source_path,
                "chunk_index": d.chunk_index,
                "source_sha256": d.source_sha256,
            }
            for d in docs
        ]
        self._collection.upsert(ids=ids, documents=texts, metadatas=metadatas)
        return len(docs)

    def delete(self, doc_ids: Iterable[str]) -> int:
        ids = list(doc_ids)
        if not ids:
            return 0
        self._collection.delete(ids=ids)
        return len(ids)

    def delete_by_source(self, source_paths: Iterable[str]) -> int:
        paths = list(source_paths)
        if not paths:
            return 0
        # Chroma supports `where` clauses on metadata.
        before = self._collection.count()
        self._collection.delete(where={"source_path": {"$in": paths}})
        after = self._collection.count()
        return before - after

    def query(self, query_text: str, *, n_results: int = 5) -> list[QueryResult]:
        if not query_text.strip():
            return []
        raw = self._collection.query(
            query_texts=[query_text],
            n_results=n_results,
        )
        out: list[QueryResult] = []
        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        for _id, text, meta, dist in zip(ids, documents, metadatas, distances):
            meta = meta or {}
            out.append(QueryResult(
                document=Document(
                    source_path=str(meta.get("source_path") or "unknown"),
                    chunk_index=int(meta.get("chunk_index") or 0),
                    text=text or "",
                    source_sha256=str(meta.get("source_sha256") or ""),
                ),
                distance=float(dist),
            ))
        return out

    def count(self) -> int:
        return self._collection.count()

    def all_doc_ids(self) -> set[str]:
        # Chroma's get() with no filter returns every row. For very large
        # collections (>>100k) this becomes slow; revisit if a skill needs
        # incremental reconciliation on a huge corpus.
        result = self._collection.get(include=[])
        ids = result.get("ids") or []
        return set(ids)
