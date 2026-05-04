"""Document loader + chunker.

Reads .md / .txt / .markdown files from a content root, chunks each
file by paragraph (with a maximum chunk size), and emits Document
objects ready for the indexer.

Chunking strategy: paragraph-first with a hard char cap. Splitting on
``\\n\\n`` preserves semantic units (paragraphs, list items, headings)
which the embedding model handles better than naive token windows.
Long paragraphs get split at sentence boundaries to fit the cap.

Future: PDF + DOCX loaders, configurable chunkers (token vs char).
For now, narrow + correct beats wide + buggy.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path

from app.rag.models import Document

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_CHUNK_CHARS = 1500
DEFAULT_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})

_PARA_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def load_documents(
    *,
    content_root: Path,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    skip_dotfiles: bool = True,
) -> Iterator[Document]:
    """Walk ``content_root``, yield Document chunks for every supported file.

    ``content_root`` paths in returned Documents are RELATIVE to the
    content_root — keeps the index portable across machines.
    """

    if not content_root.exists():
        raise FileNotFoundError(f"content_root does not exist: {content_root}")
    if not content_root.is_dir():
        raise NotADirectoryError(f"content_root is not a directory: {content_root}")

    for path in sorted(content_root.rglob("*")):
        if not path.is_file():
            continue
        if skip_dotfiles and any(part.startswith(".") for part in path.parts):
            continue
        if path.suffix.lower() not in extensions:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            LOGGER.warning("skipping %s: not valid UTF-8 (%s)", path, exc)
            continue
        if not raw.strip():
            continue

        rel = path.relative_to(content_root).as_posix()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        chunks = list(chunk_text(raw, max_chars=max_chunk_chars))
        for i, chunk in enumerate(chunks):
            yield Document(
                source_path=rel,
                chunk_index=i,
                text=chunk,
                source_sha256=sha,
            )


def chunk_text(text: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> Iterator[str]:
    """Split text into chunks <= max_chars, preferring paragraph boundaries."""

    paragraphs = [p.strip() for p in _PARA_RE.split(text) if p.strip()]
    buf: list[str] = []
    buf_len = 0
    for para in paragraphs:
        if len(para) > max_chars:
            # Flush whatever we've buffered first
            if buf:
                yield "\n\n".join(buf)
                buf, buf_len = [], 0
            # Then split the oversized paragraph at sentence boundaries
            yield from _split_oversized_paragraph(para, max_chars=max_chars)
            continue
        added_len = len(para) + (2 if buf else 0)  # \n\n separator
        if buf_len + added_len > max_chars:
            yield "\n\n".join(buf)
            buf, buf_len = [], 0
        buf.append(para)
        buf_len += added_len if buf_len else len(para)
    if buf:
        yield "\n\n".join(buf)


def _split_oversized_paragraph(para: str, *, max_chars: int) -> Iterator[str]:
    """Fallback: paragraph too long, split at sentence boundaries."""

    sentences = _SENTENCE_RE.split(para) if "." in para else [para]
    buf: list[str] = []
    buf_len = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_chars:
            # Last resort: split mid-sentence at char boundary
            if buf:
                yield " ".join(buf)
                buf, buf_len = [], 0
            for start in range(0, len(sent), max_chars):
                yield sent[start:start + max_chars]
            continue
        added = len(sent) + (1 if buf else 0)
        if buf_len + added > max_chars:
            yield " ".join(buf)
            buf, buf_len = [], 0
        buf.append(sent)
        buf_len += added if buf_len else len(sent)
    if buf:
        yield " ".join(buf)
