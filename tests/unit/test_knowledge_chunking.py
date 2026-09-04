"""Unit coverage for deterministic Markdown chunking and stable identities."""

import pytest

from packages.rag.chunking import (
    chunk_markdown,
    content_hash,
    estimate_tokens,
    extract_title,
    stable_chunk_id,
    stable_document_id,
)


def test_chunking_is_deterministic_with_overlap() -> None:
    text = " ".join(f"word-{index}" for index in range(2000))
    first = chunk_markdown(text, chunk_tokens=600, overlap_tokens=100)
    second = chunk_markdown(text, chunk_tokens=600, overlap_tokens=100)
    assert first == second
    assert len(first) >= 2
    # Overlap repeats trailing content of the previous chunk somewhere near its end.
    overlap = set(first[0].split()) & set(first[1].split())
    assert len(overlap) > 10
    assert first[1].split()[0] in set(first[0].split())


def test_chunking_short_document_yields_single_chunk() -> None:
    chunks = chunk_markdown("# Title\nShort body.", chunk_tokens=600, overlap_tokens=100)
    assert chunks == ["# Title Short body."]


def test_chunking_empty_document_yields_no_chunks() -> None:
    assert chunk_markdown("   \n", chunk_tokens=600, overlap_tokens=100) == []


def test_chunking_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        chunk_markdown("text", chunk_tokens=0, overlap_tokens=0)
    with pytest.raises(ValueError):
        chunk_markdown("text", chunk_tokens=100, overlap_tokens=100)


def test_chunk_defaults_cover_configured_range() -> None:
    from packages.config import Settings

    settings = Settings(_env_file=None)
    assert 500 <= settings.knowledge_chunk_tokens <= 800
    assert settings.knowledge_chunk_overlap_tokens == 100
    assert settings.knowledge_top_k == 8


def test_hashing_and_ids_are_stable() -> None:
    content = "# Runbook\nPayment latency."
    assert content_hash(content) == content_hash(content)
    assert len(content_hash(content)) == 64
    assert stable_document_id("knowledge/runbooks/a.md") == stable_document_id(
        "knowledge/runbooks/a.md"
    )
    assert stable_document_id("knowledge/runbooks/a.md") != stable_document_id(
        "knowledge/runbooks/b.md"
    )
    doc_id = stable_document_id("knowledge/runbooks/a.md")
    assert doc_id.startswith("DOC-")
    chunk_id = stable_chunk_id(doc_id, 0, "chunk text")
    assert chunk_id.startswith("KNW-")
    assert chunk_id == stable_chunk_id(doc_id, 0, "chunk text")
    assert chunk_id != stable_chunk_id(doc_id, 1, "chunk text")


def test_estimate_tokens_scales_with_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 8) == 2


def test_extract_title_prefers_heading() -> None:
    assert extract_title("# Payment Runbook\nBody", fallback="fallback") == "Payment Runbook"
    assert extract_title("plain first line\nsecond", fallback="fallback") == "plain first line"
