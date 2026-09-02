"""Typed contracts for the demo checkout service chain."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

CustomerId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    ),
]
Sku = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    ),
]
Quantity = Annotated[int, Field(ge=1, le=100)]
MoneyCents = Annotated[int, Field(ge=1, le=1_000_000_000)]
UnitPriceCents = Annotated[int, Field(ge=1, le=10_000_000)]
RequestId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class StrictModel(BaseModel):
    """Forbid silent contract drift at service boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckoutStatus(StrEnum):
    """Terminal status supported by the Stage 01 checkout flow."""

    CONFIRMED = "confirmed"


class ReservationStatus(StrEnum):
    """Inventory reservation status supported in Stage 01."""

    RESERVED = "reserved"


class CheckoutRequest(StrictModel):
    """Public checkout request accepted by the gateway."""

    customer_id: CustomerId
    sku: Sku
    quantity: Quantity


class OrderRequest(CheckoutRequest):
    """Gateway-to-order-service request."""

    order_id: UUID


class ReservationRequest(StrictModel):
    """Order-to-inventory-service request."""

    order_id: UUID
    sku: Sku
    quantity: Quantity


class ReservationResponse(StrictModel):
    """Successful inventory reservation."""

    request_id: RequestId
    reservation_id: UUID
    order_id: UUID
    sku: Sku
    quantity: Quantity
    unit_price_cents: UnitPriceCents
    status: ReservationStatus = ReservationStatus.RESERVED


class PaymentRequest(StrictModel):
    """Order-to-payment-service request."""

    order_id: UUID
    reservation_id: UUID
    customer_id: CustomerId
    sku: Sku
    quantity: Quantity
    unit_price_cents: UnitPriceCents


class StoredPayment(StrictModel):
    """Canonical payment/order result stored in PostgreSQL."""

    payment_id: UUID
    order_id: UUID
    reservation_id: UUID
    customer_id: CustomerId
    sku: Sku
    quantity: Quantity
    unit_price_cents: UnitPriceCents
    total_cents: MoneyCents
    status: CheckoutStatus
    created_at: AwareDatetime
    idempotent_replay: bool


class PaymentResponse(StoredPayment):
    """Payment service response with correlation metadata."""

    request_id: RequestId


class OrderResponse(PaymentResponse):
    """Order service response after inventory and payment succeed."""


class CheckoutResponse(OrderResponse):
    """Public gateway response for a confirmed checkout."""
