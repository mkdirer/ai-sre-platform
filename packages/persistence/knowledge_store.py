"""Async PostgreSQL persistence for versioned knowledge with pgvector retrieval."""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.config import Settings
from packages.models.knowledge import (
    ChunkId,
    KnowledgeChunk,
    KnowledgeDocType,
    KnowledgeDocument,
    KnowledgeHit,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgePage,
)
from packages.persistence.knowledge_rows import (
    EMBEDDING_DIMENSIONS,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
)
from packages.rag.chunking import (
    content_hash,
    estimate_tokens,
    stable_chunk_id,
    stable_document_id,
)
from packages.rag.embeddings import EmbeddingDimensionMismatch
from packages.telemetry import redact_value


class KnowledgeStoreUnavailable(Exception):
    """PostgreSQL could not safely complete a knowledge operation."""


class SqlAlchemyKnowledgeStore:
    """Idempotent versioned knowledge store with bounded cosine retrieval."""

    def __init__(self, settings: Settings, *, engine: AsyncEngine | None = None) -> None:
        from packages.persistence.database import create_database_engine

        if settings.knowledge_embedding_dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                "knowledge_embedding_dimensions="
                f"{settings.knowledge_embedding_dimensions} does not match "
                f"the provisioned pgvector column ({EMBEDDING_DIMENSIONS}); "
                "changing dimensions requires a new migration"
            )
        self._engine: AsyncEngine = engine or create_database_engine(settings)
        self._dimensions = settings.knowledge_embedding_dimensions
        self._max_top_k = settings.knowledge_max_top_k
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def ingest(
        self,
        request: KnowledgeIngestRequest,
        *,
        chunks: list[str],
        embeddings: list[list[float]],
        ingested_at: datetime | None = None,
    ) -> KnowledgeIngestResponse:
        """Upsert one document version and replace its chunks idempotently.

        Re-ingestion with an unchanged content hash and version is a no-op, so
        changing chunking configuration alone does not re-chunk stored
        documents; bump the document version to force re-chunking.
        """

        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        for vector in embeddings:
            if len(vector) != self._dimensions:
                raise EmbeddingDimensionMismatch(
                    f"embedding dimension {len(vector)} != configured {self._dimensions}"
                )
        digest = content_hash(request.content)
        document_id = stable_document_id(request.source_path)
        now = (ingested_at or datetime.now(UTC)).astimezone(UTC)
        metadata = _safe_metadata(request.doc_metadata)
        try:
            async with self._sessions() as session, session.begin():
                existing = await session.get(KnowledgeDocumentRow, document_id)
                if (
                    existing is not None
                    and existing.content_hash == digest
                    and existing.version == request.version
                ):
                    existing_ids = await _chunk_ids_for_document(session, document_id)
                    document = _to_document(existing)
                    return KnowledgeIngestResponse(
                        document=document, created=False, updated=False, chunk_ids=existing_ids
                    )
                created = existing is None
                updated = existing is not None and (
                    existing.content_hash != digest or existing.version != request.version
                )
                if existing is None:
                    session.add(
                        KnowledgeDocumentRow(
                            id=document_id,
                            source_path=request.source_path,
                            doc_type=request.doc_type.value,
                            version=request.version,
                            content_hash=digest,
                            title=request.title,
                            doc_metadata=metadata,
                            chunk_count=len(chunks),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    existing.doc_type = request.doc_type.value
                    existing.version = request.version
                    existing.content_hash = digest
                    existing.title = request.title
                    existing.doc_metadata = metadata
                    existing.chunk_count = len(chunks)
                    existing.updated_at = now
                    await session.execute(
                        delete(KnowledgeChunkRow).where(
                            KnowledgeChunkRow.document_id == document_id
                        )
                    )
                new_chunk_ids: list[ChunkId] = []
                for index, (chunk_text, vector) in enumerate(zip(chunks, embeddings, strict=True)):
                    chunk_id = stable_chunk_id(document_id, index, chunk_text)
                    new_chunk_ids.append(chunk_id)
                    session.add(
                        KnowledgeChunkRow(
                            id=chunk_id,
                            document_id=document_id,
                            source_path=request.source_path,
                            doc_type=request.doc_type.value,
                            version=request.version,
                            chunk_index=index,
                            text=chunk_text,
                            embedding=vector,
                            token_estimate=estimate_tokens(chunk_text),
                            created_at=now,
                        )
                    )
                await session.flush()
                row = await session.get(KnowledgeDocumentRow, document_id)
                assert row is not None
                document = _to_document(row)
        except (SQLAlchemyError, ValueError) as error:
            if isinstance(error, EmbeddingDimensionMismatch | ValueError) and not isinstance(
                error, SQLAlchemyError
            ):
                raise
            raise KnowledgeStoreUnavailable("knowledge ingestion failed") from error
        return KnowledgeIngestResponse(
            document=document, created=created, updated=updated, chunk_ids=new_chunk_ids
        )

    async def remove(self, source_path: str) -> bool:
        """Delete one source path and its chunks; return True when a row existed."""

        document_id = stable_document_id(source_path)
        try:
            async with self._sessions() as session, session.begin():
                existing = await session.get(KnowledgeDocumentRow, document_id)
                if existing is None:
                    return False
                await session.execute(
                    delete(KnowledgeChunkRow).where(KnowledgeChunkRow.document_id == document_id)
                )
                await session.delete(existing)
        except SQLAlchemyError as error:
            raise KnowledgeStoreUnavailable("knowledge removal failed") from error
        return True

    async def get_document(self, source_path: str) -> KnowledgeDocument | None:
        """Return one document by source path."""

        document_id = stable_document_id(source_path)
        try:
            async with self._sessions() as session:
                row = await session.get(KnowledgeDocumentRow, document_id)
        except SQLAlchemyError as error:
            raise KnowledgeStoreUnavailable("knowledge lookup failed") from error
        return _to_document(row) if row is not None else None

    async def list_documents(
        self,
        *,
        limit: int,
        offset: int,
        doc_type: KnowledgeDocType | None = None,
    ) -> tuple[KnowledgeDocument, ...]:
        """List documents newest-first in a bounded page."""

        filters = []
        if doc_type is not None:
            filters.append(KnowledgeDocumentRow.doc_type == doc_type.value)
        try:
            async with self._sessions() as session:
                rows = (
                    await session.execute(
                        select(KnowledgeDocumentRow)
                        .where(*filters)
                        .order_by(
                            KnowledgeDocumentRow.updated_at.desc(),
                            KnowledgeDocumentRow.id.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                ).scalars()
                return tuple(_to_document(row) for row in rows)
        except SQLAlchemyError as error:
            raise KnowledgeStoreUnavailable("knowledge listing failed") from error

    async def search(
        self,
        query_embedding: list[float],
        *,
        doc_types: list[KnowledgeDocType] | None = None,
        top_k: int,
    ) -> KnowledgePage:
        """Return bounded cosine-ranked hits; empty filters match all allowlisted types."""

        if len(query_embedding) != self._dimensions:
            raise EmbeddingDimensionMismatch(
                f"query dimension {len(query_embedding)} != configured {self._dimensions}"
            )
        bounded = max(1, min(top_k, self._max_top_k))
        type_values = [item.value for item in doc_types] if doc_types else None
        try:
            async with self._sessions() as session:
                statement = (
                    select(
                        KnowledgeChunkRow,
                        KnowledgeDocumentRow.title,
                        func.coalesce(
                            KnowledgeChunkRow.embedding.op("<=>")(query_embedding), 2.0
                        ).label("distance"),
                    )
                    .join(
                        KnowledgeDocumentRow,
                        KnowledgeDocumentRow.id == KnowledgeChunkRow.document_id,
                    )
                    .order_by(text("distance ASC"))
                    .limit(bounded)
                )
                if type_values is not None:
                    statement = statement.where(KnowledgeChunkRow.doc_type.in_(type_values))
                rows = (await session.execute(statement)).all()
                items: list[KnowledgeHit] = []
                for chunk_row, title, distance in rows:
                    assert isinstance(chunk_row, KnowledgeChunkRow)
                    distance_value = float(distance)
                    similarity = max(-1.0, min(1.0, 1.0 - distance_value))
                    items.append(
                        KnowledgeHit(
                            chunk_id=chunk_row.id,
                            document_id=chunk_row.document_id,
                            source_path=chunk_row.source_path,
                            doc_type=KnowledgeDocType(chunk_row.doc_type),
                            version=chunk_row.version,
                            title=title,
                            chunk_index=chunk_row.chunk_index,
                            text=chunk_row.text,
                            similarity=similarity,
                            distance=distance_value,
                        )
                    )
        except SQLAlchemyError as error:
            raise KnowledgeStoreUnavailable("knowledge search failed") from error
        return KnowledgePage(items=items, total=len(items), limit=bounded, offset=0)

    async def count_chunks(self) -> int:
        """Return the total chunk count (used by smoke/observability checks)."""

        try:
            async with self._sessions() as session:
                return int(
                    (await session.execute(select(func.count(KnowledgeChunkRow.id)))).scalar_one()
                )
        except SQLAlchemyError as error:
            raise KnowledgeStoreUnavailable("knowledge count failed") from error

    async def close(self) -> None:
        """Release pooled database connections."""

        await self._engine.dispose()


def _safe_metadata(value: dict[str, object]) -> dict[str, object]:
    redacted = redact_value(value, max_depth=4, max_collection_items=32)
    if not isinstance(redacted, dict):
        raise ValueError("knowledge metadata must be a JSON object")
    return cast(dict[str, object], redacted)


def _to_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row.id,
        source_path=row.source_path,
        doc_type=KnowledgeDocType(row.doc_type),
        version=row.version,
        content_hash=row.content_hash,
        title=row.title,
        doc_metadata=dict(row.doc_metadata),
        chunk_count=row.chunk_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_chunk(row: KnowledgeChunkRow) -> KnowledgeChunk:
    embedding = list(row.embedding) if isinstance(row.embedding, list | tuple) else []
    return KnowledgeChunk(
        id=row.id,
        document_id=row.document_id,
        source_path=row.source_path,
        doc_type=KnowledgeDocType(row.doc_type),
        version=row.version,
        chunk_index=row.chunk_index,
        text=row.text,
        embedding=[float(item) for item in embedding],
        token_estimate=row.token_estimate,
        created_at=row.created_at,
    )


async def _chunk_ids_for_document(self_session: AsyncSession, document_id: str) -> list[ChunkId]:
    rows = (
        await self_session.execute(
            select(KnowledgeChunkRow.id)
            .where(KnowledgeChunkRow.document_id == document_id)
            .order_by(KnowledgeChunkRow.chunk_index.asc())
        )
    ).scalars()
    return list(rows)


__all__ = ["KnowledgeStoreUnavailable", "SqlAlchemyKnowledgeStore"]
