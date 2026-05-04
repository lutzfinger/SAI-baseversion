"""Agent tools for the RAG channel (#sai-rag).

Per #16f the agent execution plane has bounded tools — the LLM can
only do what its registered tools allow. RAG tools are read-only
(no proposals, no side effects); the operator's question + the
agent's distilled answer go to Slack, the underlying chunks come
from the operator's private RAG index.

Surface declared in this module + ``app/agents/sai_rag_agent.surface.yaml``
(future). Tool names registered in ``REGISTERED_RAG_TOOL_SPECS``
below; the agent runner reads this list and constructs the LLM-side
schema at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.rag.query import query as rag_query
from app.rag.store import VectorStore


@dataclass
class RagToolContext:
    """Per-invocation context for the RAG tools.

    ``collections``: dict mapping collection-id (the operator-friendly
    name they reference in chat) to a constructed VectorStore.
    """

    collections: dict[str, VectorStore] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)


# ── Pydantic input models (per #6a — tools validate inputs) ──────────


class QueryRagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str = Field(..., min_length=1, max_length=80)
    question: str = Field(..., min_length=1, max_length=2000)
    n_results: int = Field(default=5, ge=1, le=20)
    max_distance: float | None = Field(default=None, ge=0.0)


class ListRagCollectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # No params; surface declared so the LLM can call the tool with {}.


# ── Tool builders ────────────────────────────────────────────────────


def _build_query_rag(ctx: RagToolContext) -> StructuredTool:
    def _query_rag(
        collection: str,
        question: str,
        n_results: int = 5,
        max_distance: float | None = None,
    ) -> dict[str, Any]:
        if collection not in ctx.collections:
            return {
                "error": "unknown_collection",
                "available": sorted(ctx.collections.keys()),
                "message": (
                    f"No collection named {collection!r}. "
                    f"Available: {sorted(ctx.collections.keys())}"
                ),
            }
        store = ctx.collections[collection]
        results = rag_query(
            store=store,
            question=question,
            n_results=n_results,
            max_distance=max_distance,
        )
        return {
            "collection": collection,
            "question": question,
            "n_results": len(results),
            "results": [
                {
                    "source_path": r.document.source_path,
                    "chunk_index": r.document.chunk_index,
                    "distance": round(r.distance, 4),
                    "text": r.document.text,
                }
                for r in results
            ],
        }

    return StructuredTool.from_function(
        name="query_rag",
        description=(
            "Run a similarity query against a configured RAG collection. "
            "Returns top-N passages with source paths + distance scores. "
            "Use this when the operator asks a question that needs grounding "
            "in their own writing / documents (e.g. 'what did I say about X?', "
            "'find references to Y in my course materials'). "
            "Always cite source_path in your reply so the operator can verify."
        ),
        args_schema=QueryRagInput,
        func=_query_rag,
    )


def _build_list_rag_collections(ctx: RagToolContext) -> StructuredTool:
    def _list_rag_collections() -> dict[str, Any]:
        return {
            "collections": [
                {
                    "id": cid,
                    "collection_name": store.collection_name,
                    "count": store.count(),
                }
                for cid, store in sorted(ctx.collections.items())
            ],
        }

    return StructuredTool.from_function(
        name="list_rag_collections",
        description=(
            "List the RAG collections available to query. Use this once "
            "before query_rag if you're unsure which collection holds the "
            "operator's answer (e.g. 'course materials' vs 'archived briefings')."
        ),
        args_schema=ListRagCollectionsInput,
        func=_list_rag_collections,
    )


def build_rag_tools(ctx: RagToolContext) -> list[StructuredTool]:
    """Build the StructuredTool list for the RAG agent."""
    return [
        _build_query_rag(ctx),
        _build_list_rag_collections(ctx),
    ]


# ── Tool spec registry (mirrors REGISTERED_TOOL_SPECS in tools.py) ────


@dataclass
class RagToolSpec:
    name: str
    rights: str  # always read_only for RAG
    one_liner: str


REGISTERED_RAG_TOOL_SPECS: list[RagToolSpec] = [
    RagToolSpec("query_rag", "read_only",
                "Similarity query over a configured RAG collection"),
    RagToolSpec("list_rag_collections", "read_only",
                "List configured RAG collections + doc counts"),
]
