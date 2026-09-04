"""Alembic environment for async PostgreSQL migrations."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.persistence.approval_rows import ApprovalRow  # noqa: F401
from packages.persistence.database import Base, build_database_url
from packages.persistence.evidence_rows import DeploymentRow, EvidenceRow  # noqa: F401
from packages.persistence.incident_rows import (  # noqa: F401
    AlertOccurrenceRow,
    AuditEventRow,
    IncidentRow,
    InvestigationRunRow,
    QueueJobRow,
)
from packages.persistence.investigation_rows import (  # noqa: F401
    HypothesisRow,
    IncidentReportRow,
    InvestigationFailureRow,
    InvestigatorCallRow,
    RecommendationRow,
)
from packages.persistence.knowledge_rows import (  # noqa: F401
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
)
from packages.persistence.payment_store import PaymentRow  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables and indexes managed outside SQLAlchemy metadata: LangGraph owns its
# checkpoint tables via AsyncPostgresSaver, and the pgvector HNSW index is
# created with raw SQL (pgvector has no portable Index construct here).
# Excluding them keeps `alembic check` honest about schema we own.
_MANAGED_ELSEWHERE_TABLES = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
)
_MANAGED_ELSEWHERE_INDEXES = frozenset({"ix_knowledge_chunks_embedding_hnsw"})


def _include_object(
    obj: object, name: str, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    """Filter externally-managed objects out of autogenerate comparisons."""

    del obj, reflected, compare_to
    if type_ == "table" and name in _MANAGED_ELSEWHERE_TABLES:
        return False
    return not (type_ == "index" and name in _MANAGED_ELSEWHERE_INDEXES)


def _database_url() -> str:
    configured = config.attributes.get("database_url")
    if isinstance(configured, str):
        return configured
    return build_database_url(Settings())


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations through an async SQLAlchemy connection."""

    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
