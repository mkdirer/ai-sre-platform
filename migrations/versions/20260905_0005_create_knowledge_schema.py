"""Create versioned knowledge documents and pgvector chunks.

Revision ID: 20260905_0005
Revises: 20260904_0004
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0005"
down_revision: str | None = "20260904_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    """Enable pgvector and add knowledge tables with a cosine HNSW index."""

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=24), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("doc_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "doc_type IN ('runbook','architecture','known_issue','prior_incident')",
            name="ck_knowledge_documents_valid_doc_type",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_documents"),
        sa.UniqueConstraint("source_path", name="uq_knowledge_documents_source_path"),
    )
    op.create_index("ix_knowledge_documents_doc_type", "knowledge_documents", ["doc_type"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("document_id", sa.String(length=24), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunks_nonnegative_index"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_knowledge_chunks_document_id_knowledge_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_chunks"),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index(
        "ix_knowledge_chunks_document_order",
        "knowledge_chunks",
        ["document_id", "chunk_index"],
    )
    op.create_index("ix_knowledge_chunks_doc_type", "knowledge_chunks", ["doc_type"])
    op.create_index("ix_knowledge_chunks_source_path", "knowledge_chunks", ["source_path"])
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    """Remove knowledge tables (pgvector extension is retained)."""

    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
    op.drop_index("ix_knowledge_chunks_source_path", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_doc_type", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_order", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_doc_type", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
