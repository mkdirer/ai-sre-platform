"""SQLAlchemy engine and metadata configuration."""

from sqlalchemy import URL, MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from packages.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared with Alembic metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_database_url(settings: Settings) -> str:
    """Build a correctly escaped async PostgreSQL URL without logging it."""

    url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )
    return url.render_as_string(hide_password=False)


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Create a lazy async engine; no connection is made during import."""

    return create_async_engine(
        build_database_url(settings),
        pool_pre_ping=True,
        connect_args={"timeout": settings.database_connect_timeout_seconds},
    )
