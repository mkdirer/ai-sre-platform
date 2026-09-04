"""PostgreSQL integration coverage for pgvector knowledge storage."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.models.knowledge import KnowledgeDocType, KnowledgeIngestRequest
from packages.persistence import SqlAlchemyKnowledgeStore
from packages.rag.chunking import chunk_markdown
from packages.rag.embeddings import (
    DeterministicFakeEmbeddingProvider,
    EmbeddingDimensionMismatch,
)

CONTENT = "# Payment\nPayment database latency and pool saturation guidance. " * 60
CPU_CONTENT = "# CPU\nOrder service CPU saturation without database involvement. " * 60


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test")


def _request(
    path: str = "knowledge/runbooks/payment.md",
    doc_type: KnowledgeDocType = KnowledgeDocType.RUNBOOK,
    version: str = "v1",
    content: str = CONTENT,
) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        source_path=path,
        doc_type=doc_type,
        version=version,
        title="Payment",
        content=content,
        doc_metadata={"service": "payment-service"},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_ingest_retrieve_and_remove(
    migrated_test_database_url: str,
) -> None:
    engine = create_async_engine(migrated_test_database_url)
    settings = _settings()
    store = SqlAlchemyKnowledgeStore(settings, engine=engine)
    provider = DeterministicFakeEmbeddingProvider(
        dimensions=settings.knowledge_embedding_dimensions
    )
    try:
        request = _request()
        chunks = chunk_markdown(
            request.content,
            chunk_tokens=settings.knowledge_chunk_tokens,
            overlap_tokens=settings.knowledge_chunk_overlap_tokens,
        )
        assert chunks
        embeddings = await provider.embed(chunks)
        first = await store.ingest(request, chunks=chunks, embeddings=embeddings)
        replay = await store.ingest(request, chunks=chunks, embeddings=embeddings)
        assert first.created is True
        assert replay.created is False and replay.updated is False
        assert first.chunk_ids == replay.chunk_ids

        vectors = await provider.embed(["payment database latency"])
        page = await store.search(vectors[0], doc_types=None, top_k=8)
        assert page.items
        assert page.items[0].chunk_id in first.chunk_ids

        runbooks = await store.search(vectors[0], doc_types=[KnowledgeDocType.RUNBOOK], top_k=8)
        assert {hit.doc_type for hit in runbooks.items} == {KnowledgeDocType.RUNBOOK}

        removed = await store.remove(request.source_path)
        assert removed is True
        assert await store.get_document(request.source_path) is None
        assert await store.remove(request.source_path) is False
    finally:
        await store.close()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_version_update_and_dimension_mismatch(
    migrated_test_database_url: str,
) -> None:
    engine = create_async_engine(migrated_test_database_url)
    settings = _settings()
    store = SqlAlchemyKnowledgeStore(settings, engine=engine)
    provider = DeterministicFakeEmbeddingProvider(
        dimensions=settings.knowledge_embedding_dimensions
    )
    try:
        request = _request(path="knowledge/runbooks/versioned.md", content="version one " * 300)
        chunks = chunk_markdown(
            request.content,
            chunk_tokens=settings.knowledge_chunk_tokens,
            overlap_tokens=settings.knowledge_chunk_overlap_tokens,
        )
        first = await store.ingest(request, chunks=chunks, embeddings=await provider.embed(chunks))
        updated_request = _request(
            path="knowledge/runbooks/versioned.md",
            version="v2",
            content="version two pool saturation " * 300,
        )
        updated_chunks = chunk_markdown(
            updated_request.content,
            chunk_tokens=settings.knowledge_chunk_tokens,
            overlap_tokens=settings.knowledge_chunk_overlap_tokens,
        )
        second = await store.ingest(
            updated_request,
            chunks=updated_chunks,
            embeddings=await provider.embed(updated_chunks),
        )
        assert second.updated is True
        assert first.chunk_ids != second.chunk_ids

        with pytest.raises(EmbeddingDimensionMismatch):
            await store.ingest(
                _request(path="knowledge/runbooks/bad-dim.md"),
                chunks=["text"],
                embeddings=[[1.0, 2.0]],
            )
        with pytest.raises(EmbeddingDimensionMismatch):
            await store.search([1.0, 2.0], doc_types=None, top_k=8)
    finally:
        await store.close()
        await engine.dispose()
