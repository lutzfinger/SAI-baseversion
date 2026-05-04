"""Typed shapes for the RAG primitive.

Per #6a every input + output across the RAG boundary uses a
Pydantic model with ``extra="forbid"``. Skills that consume RAG
results get typed objects, not raw dicts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Document(BaseModel):
    """One chunk in the index. Identity = (source_path, chunk_index)."""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        ...,
        description="Path RELATIVE to the content root the document was loaded from.",
    )
    chunk_index: int = Field(
        ...,
        ge=0,
        description="Zero-based chunk index within the source file.",
    )
    text: str = Field(..., min_length=1)
    source_sha256: str = Field(
        ...,
        description="SHA-256 of the WHOLE source file (not just this chunk). "
                    "Used by the indexer to skip unchanged files on next run.",
    )

    @property
    def doc_id(self) -> str:
        """Stable identifier the vector store uses as primary key."""
        return f"{self.source_path}::chunk::{self.chunk_index}"


class QueryResult(BaseModel):
    """One hit from a similarity query."""

    model_config = ConfigDict(extra="forbid")

    document: Document
    distance: float = Field(
        ...,
        description="Lower = more similar. Vendor-defined scale "
                    "(cosine for chromadb default).",
    )
