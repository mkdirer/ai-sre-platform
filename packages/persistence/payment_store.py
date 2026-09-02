"""Durable, concurrency-safe checkout payment storage."""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from packages.config import Settings
from packages.models.checkout import CheckoutStatus, PaymentRequest, StoredPayment
from packages.persistence.database import Base, create_database_engine


class IdempotencyConflict(Exception):
    """An idempotency key or order identity conflicts with a durable checkout."""


class PaymentStoreUnavailable(Exception):
    """PostgreSQL could not safely complete a persistence operation."""


class PaymentRow(Base):
    """Relational representation of a confirmed payment and order result."""

    __tablename__ = "checkout_payments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_checkout_payments_idempotency_key"),
        UniqueConstraint("order_id", name="uq_checkout_payments_order_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_cents > 0", name="unit_price_positive"),
        CheckConstraint("total_cents > 0", name="total_positive"),
        CheckConstraint("status = 'confirmed'", name="status_confirmed"),
    )

    payment_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    reservation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class SqlAlchemyPaymentStore:
    """PostgreSQL store using an atomic insert-or-read idempotency boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._engine = engine or create_database_engine(settings)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def create_or_get(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
    ) -> StoredPayment:
        """Insert once, return an identical replay, or reject mismatched key reuse."""

        fingerprint = _request_fingerprint(request)
        total_cents = request.quantity * request.unit_price_cents
        statement = (
            insert(PaymentRow)
            .values(
                payment_id=uuid4(),
                order_id=request.order_id,
                reservation_id=request.reservation_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                customer_id=request.customer_id,
                sku=request.sku,
                quantity=request.quantity,
                unit_price_cents=request.unit_price_cents,
                total_cents=total_cents,
                status=CheckoutStatus.CONFIRMED.value,
            )
            .on_conflict_do_nothing()
            .returning(PaymentRow)
        )

        try:
            async with self._sessions() as session, session.begin():
                inserted = (await session.execute(statement)).scalar_one_or_none()
                if inserted is not None:
                    return _to_stored_payment(inserted, idempotent_replay=False)

                existing = (
                    await session.execute(
                        select(PaymentRow).where(
                            PaymentRow.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing_order = (
                        await session.execute(
                            select(PaymentRow).where(PaymentRow.order_id == request.order_id)
                        )
                    ).scalar_one_or_none()
                    if existing_order is not None:
                        raise IdempotencyConflict
                    raise PaymentStoreUnavailable("idempotent insert completed without a row")
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflict
                return _to_stored_payment(existing, idempotent_replay=True)
        except (IdempotencyConflict, PaymentStoreUnavailable):
            raise
        except SQLAlchemyError as error:
            raise PaymentStoreUnavailable("payment persistence failed") from error

    async def get(self, payment_id: UUID) -> StoredPayment | None:
        """Fetch a persisted payment by public ID."""

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(PaymentRow).where(PaymentRow.payment_id == payment_id)
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError as error:
            raise PaymentStoreUnavailable("payment lookup failed") from error
        if row is None:
            return None
        return _to_stored_payment(row, idempotent_replay=False)

    async def is_ready(self) -> bool:
        """Check only this service's PostgreSQL dependency."""

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    async def close(self) -> None:
        """Release pooled database connections."""

        await self._engine.dispose()


def _request_fingerprint(request: PaymentRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _to_stored_payment(row: PaymentRow, *, idempotent_replay: bool) -> StoredPayment:
    return StoredPayment(
        payment_id=row.payment_id,
        order_id=row.order_id,
        reservation_id=row.reservation_id,
        customer_id=row.customer_id,
        sku=row.sku,
        quantity=row.quantity,
        unit_price_cents=row.unit_price_cents,
        total_cents=row.total_cents,
        status=CheckoutStatus(row.status),
        created_at=row.created_at,
        idempotent_replay=idempotent_replay,
    )
