"""Versioned knowledge ingestion and allowlisted retrieval (Stage 07)."""

from packages.rag.chunking import (
    chunk_markdown,
    content_hash,
    estimate_tokens,
    extract_title,
    stable_chunk_id,
    stable_document_id,
)
from packages.rag.embeddings import (
    DeterministicFakeEmbeddingProvider,
    EmbeddingDimensionMismatch,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    OpenAIEmbeddingProvider,
    UnavailableEmbeddingProvider,
    build_embedding_provider,
    validate_dimensions,
)
from packages.rag.service import (
    KnowledgeService,
    KnowledgeServiceUnavailable,
    format_knowledge_context,
    knowledge_citation_ids,
)

__all__ = [
    "DeterministicFakeEmbeddingProvider",
    "EmbeddingDimensionMismatch",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "KnowledgeService",
    "KnowledgeServiceUnavailable",
    "OpenAIEmbeddingProvider",
    "UnavailableEmbeddingProvider",
    "build_embedding_provider",
    "chunk_markdown",
    "content_hash",
    "estimate_tokens",
    "extract_title",
    "format_knowledge_context",
    "knowledge_citation_ids",
    "stable_chunk_id",
    "stable_document_id",
    "validate_dimensions",
]
