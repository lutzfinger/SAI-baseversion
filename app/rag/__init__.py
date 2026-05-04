"""RAG framework primitive (PRINCIPLES.md §33a — separately-shipped primitive).

Exposes a vendor-portable VectorStore protocol + a Chroma-backed
implementation + indexer + query functions. Skills compose these via
their cascade runner; they do NOT reach into a vendor SDK directly.

Public surface:
  * ``VectorStore`` (Protocol) — vendor-portable abstraction
  * ``ChromaVectorStore`` — concrete persistent implementation
  * ``Document`` — typed chunk with metadata (Pydantic, extra=forbid)
  * ``load_documents`` — read .md/.txt from a content dir, chunk, hash
  * ``build_or_update_index`` — incremental indexing with manifest
  * ``query`` — top-N similarity search returning Documents
  * ``IndexManifest`` — typed wrapper around the on-disk manifest

Optional dependency: ``chromadb`` (extras_require: rag). Skills that
declare a ``rag_query`` tier inherit the dependency; SAI installs
without rag work fine for non-RAG workflows.
"""

from app.rag.chroma_store import ChromaVectorStore
from app.rag.indexer import build_or_update_index
from app.rag.loader import chunk_text, load_documents
from app.rag.manifest import IndexManifest
from app.rag.models import Document, QueryResult
from app.rag.query import query
from app.rag.store import VectorStore

__all__ = [
    "ChromaVectorStore",
    "Document",
    "IndexManifest",
    "QueryResult",
    "VectorStore",
    "build_or_update_index",
    "chunk_text",
    "load_documents",
    "query",
]
