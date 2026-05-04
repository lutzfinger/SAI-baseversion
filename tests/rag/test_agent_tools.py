"""Tests for the RAG agent tools (used by the #sai-rag Slack agent)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from app.agents.rag_tools import (
    QueryRagInput,
    RagToolContext,
    build_rag_tools,
)
from app.rag import ChromaVectorStore


@pytest.fixture
def rag_ctx(tmp_path: Path) -> RagToolContext:
    store = ChromaVectorStore(
        persist_path=tmp_path / "chroma", collection_name="docs",
    )
    from app.rag.models import Document
    store.upsert([
        Document(
            source_path="cats.md", chunk_index=0,
            text="Cats are small carnivorous mammals known for independence.",
            source_sha256="x",
        ),
        Document(
            source_path="dogs.md", chunk_index=0,
            text="Dogs are loyal pack animals descended from wolves.",
            source_sha256="y",
        ),
    ])
    return RagToolContext(collections={"my_corpus": store})


def test_query_rag_returns_relevant_results(rag_ctx: RagToolContext):
    tools = build_rag_tools(rag_ctx)
    query_tool = next(t for t in tools if t.name == "query_rag")
    out = query_tool.invoke({
        "collection": "my_corpus",
        "question": "feline pets",
        "n_results": 1,
    })
    assert out["n_results"] == 1
    assert out["results"][0]["source_path"] == "cats.md"


def test_query_rag_unknown_collection_returns_error(rag_ctx: RagToolContext):
    tools = build_rag_tools(rag_ctx)
    query_tool = next(t for t in tools if t.name == "query_rag")
    out = query_tool.invoke({
        "collection": "does_not_exist",
        "question": "anything",
    })
    assert out["error"] == "unknown_collection"
    assert "my_corpus" in out["available"]


def test_list_rag_collections(rag_ctx: RagToolContext):
    tools = build_rag_tools(rag_ctx)
    list_tool = next(t for t in tools if t.name == "list_rag_collections")
    out = list_tool.invoke({})
    assert len(out["collections"]) == 1
    assert out["collections"][0]["id"] == "my_corpus"
    assert out["collections"][0]["count"] == 2


def test_query_rag_input_validates_bounds():
    """Per #6a — tool inputs use Pydantic with extra=forbid."""
    with pytest.raises(Exception):
        QueryRagInput(collection="x", question="y", n_results=999)
    with pytest.raises(Exception):
        QueryRagInput(collection="x", question="y", n_results=0)
    with pytest.raises(Exception):
        QueryRagInput(collection="", question="y")
    with pytest.raises(Exception):
        QueryRagInput(collection="x", question="y", extra_field="nope")


def test_query_rag_max_distance_filter(rag_ctx: RagToolContext):
    tools = build_rag_tools(rag_ctx)
    query_tool = next(t for t in tools if t.name == "query_rag")
    out = query_tool.invoke({
        "collection": "my_corpus",
        "question": "completely unrelated quantum chromodynamics",
        "max_distance": 0.1,  # very tight
    })
    assert out["n_results"] == 0
