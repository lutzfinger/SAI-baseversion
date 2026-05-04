"""CLI: build / update a RAG index from a content directory.

Usage:

    .venv/bin/python -m scripts.sai_rag_index \\
        --content-root <path> \\
        --persist-path <path> \\
        --collection <name>

Idempotent: safe to re-run; only changed files are re-indexed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.rag import ChromaVectorStore, build_or_update_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or incrementally update a SAI RAG index.",
    )
    parser.add_argument(
        "--content-root", type=Path, required=True,
        help="Directory of .md / .txt files to index.",
    )
    parser.add_argument(
        "--persist-path", type=Path, required=True,
        help="Where the chroma index lives on disk.",
    )
    parser.add_argument(
        "--collection", type=str, required=True,
        help="Chroma collection name (e.g. 'my-knowledge-base').",
    )
    parser.add_argument(
        "--manifest-path", type=Path, default=None,
        help="Path to the index manifest. Defaults to "
             "<persist-path>/../manifest.json.",
    )
    parser.add_argument(
        "--max-chunk-chars", type=int, default=1500,
        help="Max chars per chunk before paragraph splitting.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    manifest_path = args.manifest_path or (args.persist_path.parent / "manifest.json")

    store = ChromaVectorStore(
        persist_path=args.persist_path,
        collection_name=args.collection,
    )
    result = build_or_update_index(
        content_root=args.content_root,
        store=store,
        manifest_path=manifest_path,
        max_chunk_chars=args.max_chunk_chars,
    )
    print(
        f"index update: +{result.files_added} added, "
        f"~{result.files_changed} changed, "
        f"-{result.files_removed} removed, "
        f"={result.files_skipped_unchanged} skipped (unchanged); "
        f"+{result.chunks_upserted} chunks upserted, "
        f"-{result.chunks_deleted} chunks deleted; "
        f"total in index now: {store.count()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
