"""Order-service client boundary for the gateway."""

from typing import Protocol

import httpx

from apps.demo.common.http_client import ServiceHttpClient
from packages.config import Settings
from packages.models.checkout import OrderRequest, OrderResponse
from packages.telemetry import TelemetryRuntime


class OrderClient(Protocol):
    """Operations the gateway may perform against the order service."""

    async def create_order(
        self,
        request: OrderRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> OrderResponse: ...

    async def is_ready(self) -> bool: ...


class HttpOrderClient:
    """HTTP implementation of the gateway's order-service boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = ServiceHttpClient(
            service_name="order_service",
            base_url=str(settings.order_service_url),
            timeout_seconds=settings.outbound_http_timeout_seconds,
            max_attempts=settings.outbound_http_max_attempts,
            retry_backoff_seconds=settings.outbound_http_retry_backoff_seconds,
            telemetry=telemetry,
            transport=transport,
        )

    async def create_order(
        self,
        request: OrderRequest,
        *,
        idempotency_key: str,
        request_id: str,
    ) -> OrderResponse:
        return await self._client.post_model(
            path="/orders",
            payload=request,
            response_model=OrderResponse,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    async def is_ready(self) -> bool:
        return await self._client.is_ready()
