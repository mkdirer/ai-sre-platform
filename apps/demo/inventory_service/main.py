"""Deterministic inventory reservation service."""

from typing import Annotated
from uuid import UUID, uuid5

from fastapi import Depends, FastAPI, Request

from apps.demo.common.web import (
    ApiError,
    create_service_app,
    current_request_id,
    require_idempotency_key,
)
from packages.config import Settings
from packages.models.checkout import (
    IdempotencyKey,
    ReservationRequest,
    ReservationResponse,
)
from packages.models.http import ErrorResponse, HealthResponse

_RESERVATION_ID_NAMESPACE = UUID("a0447bf5-1d52-45c9-86fb-bce41dbe991c")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dependency-free inventory service."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Demo Inventory Service",
        service_name="inventory-service",
        settings=resolved_settings,
    )

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="inventory-service")

    @app.get("/health/ready", response_model=HealthResponse)
    async def readiness() -> HealthResponse:
        return HealthResponse(service="inventory-service")

    @app.post(
        "/reservations",
        response_model=ReservationResponse,
        responses={
            400: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    async def reserve(
        reservation_request: ReservationRequest,
        request: Request,
        _idempotency_key: Annotated[IdempotencyKey, Depends(require_idempotency_key)],
    ) -> ReservationResponse:
        if reservation_request.sku != resolved_settings.inventory_sku:
            raise ApiError(409, "unknown_sku", "requested SKU is not available")
        if reservation_request.quantity > resolved_settings.inventory_stock:
            raise ApiError(409, "insufficient_stock", "requested quantity is not available")

        reservation_id = uuid5(
            _RESERVATION_ID_NAMESPACE,
            f"{reservation_request.order_id}:{reservation_request.sku}",
        )
        return ReservationResponse(
            request_id=current_request_id(request),
            reservation_id=reservation_id,
            order_id=reservation_request.order_id,
            sku=reservation_request.sku,
            quantity=reservation_request.quantity,
            unit_price_cents=resolved_settings.inventory_unit_price_cents,
        )

    return app
