"""Order orchestration service."""

import asyncio
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

from apps.demo.common.http_client import ServiceCallError
from apps.demo.common.web import (
    ApiError,
    create_service_app,
    current_request_id,
    get_telemetry,
    require_idempotency_key,
)
from apps.demo.order_service.clients import (
    HttpInventoryClient,
    HttpPaymentClient,
    InventoryClient,
    PaymentClient,
)
from packages.config import Settings
from packages.models.checkout import (
    IdempotencyKey,
    OrderRequest,
    OrderResponse,
    PaymentRequest,
    ReservationRequest,
)
from packages.models.http import ErrorResponse, HealthResponse

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}


def create_app(
    settings: Settings | None = None,
    *,
    inventory_client: InventoryClient | None = None,
    payment_client: PaymentClient | None = None,
) -> FastAPI:
    """Build the order service with replaceable downstream clients."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Demo Order Service",
        service_name="order-service",
        settings=resolved_settings,
    )
    telemetry = get_telemetry(app)
    resolved_inventory_client = inventory_client or HttpInventoryClient(
        resolved_settings,
        telemetry=telemetry,
    )
    resolved_payment_client = payment_client or HttpPaymentClient(
        resolved_settings,
        telemetry=telemetry,
    )

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="order-service")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def readiness() -> HealthResponse:
        inventory_ready, payment_ready = await asyncio.gather(
            resolved_inventory_client.is_ready(),
            resolved_payment_client.is_ready(),
        )
        if not inventory_ready or not payment_ready:
            raise ApiError(503, "dependency_unavailable", "order service dependency is not ready")
        return HealthResponse(
            service="order-service",
            dependencies={"inventory_service": "ready", "payment_service": "ready"},
        )

    @app.post(
        "/orders",
        response_model=OrderResponse,
        responses=_ERROR_RESPONSES,
    )
    async def create_order(
        order_request: OrderRequest,
        request: Request,
        idempotency_key: Annotated[IdempotencyKey, Depends(require_idempotency_key)],
    ) -> OrderResponse:
        request_id = current_request_id(request)
        try:
            reservation = await resolved_inventory_client.reserve(
                ReservationRequest(
                    order_id=order_request.order_id,
                    sku=order_request.sku,
                    quantity=order_request.quantity,
                ),
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            payment = await resolved_payment_client.pay(
                PaymentRequest(
                    order_id=order_request.order_id,
                    reservation_id=reservation.reservation_id,
                    customer_id=order_request.customer_id,
                    sku=order_request.sku,
                    quantity=order_request.quantity,
                    unit_price_cents=reservation.unit_price_cents,
                ),
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
        except ServiceCallError as error:
            raise ApiError(error.status_code, error.code, error.message) from error
        return OrderResponse.model_validate(payment.model_dump())

    return app
