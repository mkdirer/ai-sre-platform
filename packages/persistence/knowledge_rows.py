"""SQLAlchemy mappings for versioned knowledge documents and pgvector chunks."""

from datetime import datetime

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence.database import Base

EMBEDDING_DIMENSIONS = 1536


class KnowledgeDocumentRow(Base):
    """One versioned Markdown source; chunks are replaced on content updates."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('runbook','architecture','known_issue','prior_incident')",
            name="valid_doc_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    doc_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("CURRENT_TIMESTAMP")
    )


class KnowledgeChunkRow(Base):
    """One ordered chunk with a cosine-searchable embedding."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="nonnegative_chunk_index"),
        Index("ix_knowledge_chunks_document_order", "document_id", "chunk_index"),
        Index("ix_knowledge_chunks_doc_type", "doc_type"),
    )

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("CURRENT_TIMESTAMP")
    )
