"""Inventory and payment client boundaries for the order service."""

from typing import Protocol

import httpx

from apps.demo.common.http_client import ServiceHttpClient
from packages.config import Settings
from packages.models.checkout import (
    PaymentRequest,
    PaymentResponse,
    ReservationRequest,
    ReservationResponse,
)
from packages.telemetry import TelemetryRuntime


class InventoryClient(Protocol):
    """Operations the order service may perform against inventory."""

    async def reserve(
        self,
        request: ReservationRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> ReservationResponse: ...

    async def is_ready(self) -> bool: ...


class PaymentClient(Protocol):
    """Operations the order service may perform against payment."""

    async def pay(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> PaymentResponse: ...

    async def is_ready(self) -> bool: ...


class HttpInventoryClient:
    """HTTP implementation of the inventory boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = ServiceHttpClient(
            service_name="inventory_service",
            base_url=str(settings.inventory_service_url),
            timeout_seconds=settings.outbound_http_timeout_seconds,
            max_attempts=settings.outbound_http_max_attempts,
            retry_backoff_seconds=settings.outbound_http_retry_backoff_seconds,
            telemetry=telemetry,
            transport=transport,
        )

    async def reserve(
        self,
        request: ReservationRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> ReservationResponse:
        return await self._client.post_model(
            path="/reservations",
            payload=request,
            response_model=ReservationResponse,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    async def is_ready(self) -> bool:
        return await self._client.is_ready()


class HttpPaymentClient:
    """HTTP implementation of the payment boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = ServiceHttpClient(
            service_name="payment_service",
            base_url=str(settings.payment_service_url),
            timeout_seconds=settings.outbound_http_timeout_seconds,
            max_attempts=settings.outbound_http_max_attempts,
            retry_backoff_seconds=settings.outbound_http_retry_backoff_seconds,
            telemetry=telemetry,
            transport=transport,
        )

    async def pay(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> PaymentResponse:
        return await self._client.post_model(
            path="/payments",
            payload=request,
            response_model=PaymentResponse,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    async def is_ready(self) -> bool:
        return await self._client.is_ready()
