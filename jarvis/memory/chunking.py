"""Deterministic paragraph-aware chunking for document ingestion."""

from __future__ import annotations


def chunk_text(text: str, *, max_chars: int, overlap: int = 0) -> list[str]:
    """Split text into chunks of at most ``max_chars``, preferring paragraph breaks.

    Paragraphs (blank-line separated) are packed greedily; a single paragraph
    longer than ``max_chars`` is hard-split with ``overlap`` chars of carryover
    so a fact straddling a cut survives in at least one chunk.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be >= 0 and < max_chars")

    normalized = text.strip()
    if not normalized:
        return []

    paragraphs = [p.strip() for p in normalized.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(paragraph, max_chars=max_chars, overlap=overlap))
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, *, max_chars: int, overlap: int) -> list[str]:
    step = max_chars - overlap
    pieces: list[str] = []
    start = 0
    while start < len(paragraph):
        pieces.append(paragraph[start : start + max_chars])
        if start + max_chars >= len(paragraph):
            break
        start += step
    return pieces
