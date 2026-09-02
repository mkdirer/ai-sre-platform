"""Deterministic fakes for service boundary tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid5

from packages.models.checkout import (
    CheckoutStatus,
    OrderRequest,
    OrderResponse,
    PaymentRequest,
    PaymentResponse,
    ReservationRequest,
    ReservationResponse,
    StoredPayment,
)
from packages.persistence import IdempotencyConflict

PAYMENT_ID_NAMESPACE = UUID("7e366c28-16d9-4386-a0bc-b1095cb05017")
RESERVATION_ID = UUID("1c98b449-76e8-49ca-8e21-2ec33c7c8ac5")
FIXED_CREATED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass
class FakeOrderClient:
    """Gateway dependency returning a stable confirmed order."""

    ready: bool = True
    calls: list[tuple[OrderRequest, str, str]] = field(default_factory=list)

    async def create_order(
        self,
        request: OrderRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> OrderResponse:
        self.calls.append((request, idempotency_key, request_id))
        return OrderResponse(
            request_id=request_id,
            payment_id=uuid5(PAYMENT_ID_NAMESPACE, idempotency_key),
            order_id=request.order_id,
            reservation_id=RESERVATION_ID,
            customer_id=request.customer_id,
            sku=request.sku,
            quantity=request.quantity,
            unit_price_cents=1999,
            total_cents=request.quantity * 1999,
            status=CheckoutStatus.CONFIRMED,
            created_at=FIXED_CREATED_AT,
            idempotent_replay=False,
        )

    async def is_ready(self) -> bool:
        return self.ready


@dataclass
class FakeInventoryClient:
    """Order dependency returning a deterministic reservation."""

    ready: bool = True
    calls: list[tuple[ReservationRequest, str, str]] = field(default_factory=list)

    async def reserve(
        self,
        request: ReservationRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> ReservationResponse:
        self.calls.append((request, idempotency_key, request_id))
        return ReservationResponse(
            request_id=request_id,
            reservation_id=RESERVATION_ID,
            order_id=request.order_id,
            sku=request.sku,
            quantity=request.quantity,
            unit_price_cents=1999,
        )

    async def is_ready(self) -> bool:
        return self.ready


@dataclass
class FakePaymentClient:
    """Order dependency returning a deterministic payment result."""

    ready: bool = True
    calls: list[tuple[PaymentRequest, str, str]] = field(default_factory=list)

    async def pay(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> PaymentResponse:
        self.calls.append((request, idempotency_key, request_id))
        return PaymentResponse(
            request_id=request_id,
            payment_id=uuid5(PAYMENT_ID_NAMESPACE, idempotency_key),
            order_id=request.order_id,
            reservation_id=request.reservation_id,
            customer_id=request.customer_id,
            sku=request.sku,
            quantity=request.quantity,
            unit_price_cents=request.unit_price_cents,
            total_cents=request.quantity * request.unit_price_cents,
            status=CheckoutStatus.CONFIRMED,
            created_at=FIXED_CREATED_AT,
            idempotent_replay=False,
        )

    async def is_ready(self) -> bool:
        return self.ready


class FakePaymentStore:
    """In-memory payment boundary with production-equivalent idempotency semantics."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.closed = False
        self._records: dict[str, tuple[PaymentRequest, StoredPayment]] = {}

    async def create_or_get(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
    ) -> StoredPayment:
        existing = self._records.get(idempotency_key)
        if existing is not None:
            original_request, payment = existing
            if original_request != request:
                raise IdempotencyConflict
            return payment.model_copy(update={"idempotent_replay": True})

        payment = StoredPayment(
            payment_id=uuid5(PAYMENT_ID_NAMESPACE, idempotency_key),
            order_id=request.order_id,
            reservation_id=request.reservation_id,
            customer_id=request.customer_id,
            sku=request.sku,
            quantity=request.quantity,
            unit_price_cents=request.unit_price_cents,
            total_cents=request.quantity * request.unit_price_cents,
            status=CheckoutStatus.CONFIRMED,
            created_at=FIXED_CREATED_AT,
            idempotent_replay=False,
        )
        self._records[idempotency_key] = (request, payment)
        return payment

    async def get(self, payment_id: UUID) -> StoredPayment | None:
        for _request, payment in self._records.values():
            if payment.payment_id == payment_id:
                return payment
        return None

    async def is_ready(self) -> bool:
        return self.ready

    async def close(self) -> None:
        self.closed = True
