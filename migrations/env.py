"""Alembic environment for async PostgreSQL migrations."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from packages.config import Settings
from packages.persistence.database import Base, build_database_url
from packages.persistence.evidence_rows import DeploymentRow, EvidenceRow  # noqa: F401
from packages.persistence.incident_rows import (  # noqa: F401
    AlertOccurrenceRow,
    AuditEventRow,
    IncidentRow,
    InvestigationRunRow,
    QueueJobRow,
)
from packages.persistence.payment_store import PaymentRow  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
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
