"""Deterministic Markdown chunking, hashing, and stable identity helpers."""

import hashlib
import json

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate tokens as ceiling(chars / 4) with a floor of 1 for non-empty text."""

    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def content_hash(content: str) -> str:
    """Return the canonical SHA-256 hex digest of document content."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def stable_document_id(source_path: str) -> str:
    """Derive a stable document ID from the source path (version-independent)."""

    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    return f"DOC-{digest[:20].upper()}"


def stable_chunk_id(document_id: str, chunk_index: int, chunk_text: str) -> str:
    """Derive a stable chunk ID from document identity, order, and text."""

    canonical = json.dumps(
        {"document_id": document_id, "chunk_index": chunk_index, "text": chunk_text},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"KNW-{hashlib.sha256(canonical.encode()).hexdigest()[:24].upper()}"


def chunk_markdown(text: str, *, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Split Markdown into overlapping chunks on word boundaries, deterministically.

    Token counts are approximated at 4 chars per token. Chunks preserve word
    boundaries; overlap repeats the trailing words of the previous chunk.
    """

    if not text.strip():
        return []
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must satisfy 0 <= overlap < chunk_tokens")
    chunk_chars = chunk_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for word in words:
        addition = len(word) + (1 if current else 0)
        if current and current_chars + addition > chunk_chars:
            chunks.append(" ".join(current))
            if overlap_chars <= 0:
                current = []
                current_chars = 0
            else:
                # Retain trailing words covering approximately overlap_chars.
                kept: list[str] = []
                kept_chars = 0
                for candidate in reversed(current):
                    need = len(candidate) + (1 if kept else 0)
                    if kept and kept_chars + need > overlap_chars:
                        break
                    kept.append(candidate)
                    kept_chars += need
                current = list(reversed(kept))
                current_chars = kept_chars
            addition = len(word) + (1 if current else 0)
        current.append(word)
        current_chars += addition
    if current:
        chunks.append(" ".join(current))
    return chunks


def extract_title(content: str, *, fallback: str) -> str:
    """Return the first Markdown heading, or a bounded fallback title."""

    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if line.strip().startswith("#") and stripped:
            return stripped[:256]
    first = content.strip().splitlines()
    if first and first[0].strip():
        return first[0].strip()[:256]
    return fallback[:256]


__all__ = [
    "CHARS_PER_TOKEN",
    "chunk_markdown",
    "content_hash",
    "estimate_tokens",
    "extract_title",
    "stable_chunk_id",
    "stable_document_id",
]
