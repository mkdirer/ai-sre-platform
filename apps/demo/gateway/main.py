"""Public checkout gateway."""

from typing import Annotated, Any
from uuid import UUID, uuid5

from fastapi import Depends, FastAPI, Request

from apps.demo.common.http_client import ServiceCallError
from apps.demo.common.web import (
    ApiError,
    create_service_app,
    current_request_id,
    get_telemetry,
    require_idempotency_key,
)
from apps.demo.gateway.client import HttpOrderClient, OrderClient
from packages.config import Settings
from packages.models.checkout import (
    CheckoutRequest,
    CheckoutResponse,
    IdempotencyKey,
    OrderRequest,
)
from packages.models.http import ErrorResponse, HealthResponse

_ORDER_ID_NAMESPACE = UUID("62be4d2c-d354-4e70-b58e-593f4ff4cbf1")
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
    order_client: OrderClient | None = None,
) -> FastAPI:
    """Build the gateway without opening network connections at import time."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Demo Gateway",
        service_name="gateway",
        settings=resolved_settings,
    )
    resolved_order_client = order_client or HttpOrderClient(
        resolved_settings,
        telemetry=get_telemetry(app),
    )

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="gateway")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def readiness() -> HealthResponse:
        if not await resolved_order_client.is_ready():
            raise ApiError(503, "order_service_unavailable", "order service is not ready")
        return HealthResponse(service="gateway", dependencies={"order_service": "ready"})

    @app.post(
        "/checkout",
        response_model=CheckoutResponse,
        responses=_ERROR_RESPONSES,
    )
    async def checkout(
        checkout_request: CheckoutRequest,
        request: Request,
        idempotency_key: Annotated[IdempotencyKey, Depends(require_idempotency_key)],
    ) -> CheckoutResponse:
        request_id = current_request_id(request)
        order_id = uuid5(_ORDER_ID_NAMESPACE, idempotency_key)
        order_request = OrderRequest(
            order_id=order_id,
            customer_id=checkout_request.customer_id,
            sku=checkout_request.sku,
            quantity=checkout_request.quantity,
        )
        try:
            order = await resolved_order_client.create_order(
                order_request,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
        except ServiceCallError as error:
            raise ApiError(error.status_code, error.code, error.message) from error
        return CheckoutResponse.model_validate(order.model_dump())

    return app
