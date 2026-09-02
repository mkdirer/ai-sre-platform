"""Payment service and PostgreSQL persistence boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Protocol
from uuid import UUID

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
    PaymentRequest,
    PaymentResponse,
    StoredPayment,
)
from packages.models.http import ErrorResponse, HealthResponse
from packages.persistence import (
    IdempotencyConflict,
    PaymentStoreUnavailable,
    SqlAlchemyPaymentStore,
)


class PaymentStore(Protocol):
    """Persistence operations required by the payment API."""

    async def create_or_get(
        self,
        request: PaymentRequest,
        *,
        idempotency_key: str,
    ) -> StoredPayment: ...

    async def get(self, payment_id: UUID) -> StoredPayment | None: ...

    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


def create_app(
    settings: Settings | None = None,
    *,
    store: PaymentStore | None = None,
) -> FastAPI:
    """Build the payment app with an injectable persistence store."""

    resolved_settings = settings or Settings()
    resolved_store = store or SqlAlchemyPaymentStore(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await resolved_store.close()

    app = create_service_app(title="AI SRE Demo Payment Service")
    app.router.lifespan_context = lifespan

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(service="payment-service")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def readiness() -> HealthResponse:
        if not await resolved_store.is_ready():
            raise ApiError(503, "postgres_unavailable", "PostgreSQL is not ready")
        return HealthResponse(service="payment-service", dependencies={"postgres": "ready"})

    @app.post(
        "/payments",
        response_model=PaymentResponse,
        responses={
            400: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def create_payment(
        payment_request: PaymentRequest,
        request: Request,
        idempotency_key: Annotated[IdempotencyKey, Depends(require_idempotency_key)],
    ) -> PaymentResponse:
        try:
            payment = await resolved_store.create_or_get(
                payment_request,
                idempotency_key=idempotency_key,
            )
        except IdempotencyConflict as error:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key or order was already used for a different checkout",
            ) from error
        except PaymentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "payment persistence is unavailable"
            ) from error
        return _payment_response(payment, current_request_id(request))

    @app.get(
        "/payments/{payment_id}",
        response_model=PaymentResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_payment(payment_id: UUID, request: Request) -> PaymentResponse:
        try:
            payment = await resolved_store.get(payment_id)
        except PaymentStoreUnavailable as error:
            raise ApiError(
                503, "persistence_unavailable", "payment persistence is unavailable"
            ) from error
        if payment is None:
            raise ApiError(404, "payment_not_found", "payment was not found")
        return _payment_response(payment, current_request_id(request))

    return app


def _payment_response(payment: StoredPayment, request_id: str) -> PaymentResponse:
    return PaymentResponse.model_validate(
        {**payment.model_dump(mode="python"), "request_id": request_id}
    )
