"""Payment service and PostgreSQL persistence boundary."""

from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request

from apps.demo.common.faults import (
    FaultControlDisabledError as MultiFaultDisabledError,
)
from apps.demo.common.faults import (
    FaultControlUnauthorizedError as MultiFaultUnauthorizedError,
)
from apps.demo.common.faults import MultiFaultController
from apps.demo.common.web import (
    ApiError,
    create_service_app,
    current_request_id,
    get_telemetry,
    register_shutdown_callback,
    require_idempotency_key,
)
from apps.demo.payment_service.faults import (
    FaultControlDisabledError,
    FaultControlUnauthorizedError,
    SlowDatabaseFaultController,
)
from packages.config import Settings
from packages.models.checkout import (
    IdempotencyKey,
    PaymentRequest,
    PaymentResponse,
    StoredPayment,
)
from packages.models.faults import (
    PAYMENT_FAULTS,
    FaultListResponse,
    FaultName,
    FaultStateResponse,
    FaultUpdateRequest,
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


def _parse_fault_name(raw: str) -> FaultName:
    """Parse a URL fault segment to an allowlisted payment fault."""

    normalized = raw.replace("-", "_")
    try:
        fault = FaultName(normalized)
    except ValueError as error:
        raise ApiError(404, "fault_not_found", f"unknown fault: {raw}") from error
    if fault not in PAYMENT_FAULTS:
        raise ApiError(404, "fault_not_found", f"fault not owned by payment-service: {raw}")
    return fault


def create_app(
    settings: Settings | None = None,
    *,
    store: PaymentStore | None = None,
    fault_controller: SlowDatabaseFaultController | None = None,
    multi_fault_controller: MultiFaultController | None = None,
) -> FastAPI:
    """Build the payment app with an injectable persistence store."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Demo Payment Service",
        service_name="payment-service",
        settings=resolved_settings,
    )
    telemetry = get_telemetry(app)
    resolved_store = store or SqlAlchemyPaymentStore(
        resolved_settings,
        telemetry=telemetry,
    )
    resolved_fault_controller = fault_controller or SlowDatabaseFaultController.from_settings(
        resolved_settings,
        telemetry=telemetry,
        state_callback=telemetry.metrics.set_slow_database_fault,
    )

    def _multi_state_callback(fault: FaultName, enabled: bool) -> None:
        telemetry.metrics.set_fault_enabled(fault.value, enabled)

    resolved_multi = multi_fault_controller or MultiFaultController.from_settings(
        resolved_settings,
        service_name="payment-service",
        # slow_database stays owned by the legacy controller for backwards
        # compatibility, so the multi controller must not hold a second copy
        # of that flag. The API allowlist (PAYMENT_FAULTS) still routes
        # slow_database URLs to the legacy controller.
        faults=tuple(fault for fault in PAYMENT_FAULTS if fault != FaultName.SLOW_DATABASE),
        telemetry=telemetry,
        state_callback=_multi_state_callback,
    )
    register_shutdown_callback(app, resolved_store.close)

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
            await resolved_fault_controller.inject_before_database()
            # Stage 09 simulated faults share the same guarded boundary.
            # slow_database stays owned by the legacy controller for
            # backwards compatibility; the remaining payment faults live in
            # the multi controller. All effects are bounded and reversible.
            for extra in (
                FaultName.POOL_EXHAUSTION,
                FaultName.BAD_DEPLOYMENT,
                FaultName.CPU_SATURATION,
            ):
                await resolved_multi.inject_delay(extra)
            if resolved_multi.should_inject_error(FaultName.HIGH_ERROR_RATE, idempotency_key):
                telemetry.logger.warning(
                    "fault.high_error_rate.injected",
                    extra={
                        "structured": {
                            "fault.name": FaultName.HIGH_ERROR_RATE.value,
                            "fault.enabled": True,
                        }
                    },
                )
                raise ApiError(
                    500, "simulated_high_error_rate", "simulated application error for eval"
                )
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

    def authorize_fault_control(token: str | None) -> None:
        try:
            resolved_fault_controller.authorize(token)
            resolved_multi.authorize(token)
        except (FaultControlDisabledError, MultiFaultDisabledError) as error:
            raise ApiError(
                403,
                "fault_control_disabled",
                "Fault control is disabled outside an explicitly allowed local environment",
            ) from error
        except (FaultControlUnauthorizedError, MultiFaultUnauthorizedError) as error:
            raise ApiError(
                401,
                "fault_control_unauthorized",
                "A valid X-Fault-Control-Token header is required",
            ) from error

    def _read_state(fault: FaultName) -> FaultStateResponse:
        if fault == FaultName.SLOW_DATABASE:
            return resolved_fault_controller.state()
        return resolved_multi.state(fault)

    def _write_state(fault: FaultName, enabled: bool) -> FaultStateResponse:
        if fault == FaultName.SLOW_DATABASE:
            return resolved_fault_controller.set_enabled(enabled)
        return resolved_multi.set_enabled(fault, enabled)

    # NOTE: the explicit slow-database routes must stay registered before the
    # generic /internal/faults/{fault_name} routes below. FastAPI matches in
    # registration order and the generic parser also accepts "slow-database",
    # so reordering would shadow the legacy controller.
    @app.get(
        "/internal/faults/slow-database",
        response_model=FaultStateResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def get_slow_database_fault(
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return resolved_fault_controller.state()

    @app.put(
        "/internal/faults/slow-database",
        response_model=FaultStateResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def set_slow_database_fault(
        update: FaultUpdateRequest,
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return resolved_fault_controller.set_enabled(update.enabled)

    @app.get(
        "/internal/faults",
        response_model=FaultListResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def list_payment_faults(
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultListResponse:
        authorize_fault_control(x_fault_control_token)
        items = [_read_state(fault) for fault in PAYMENT_FAULTS]
        return FaultListResponse(faults=items)

    @app.get(
        "/internal/faults/{fault_name}",
        response_model=FaultStateResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
    )
    async def get_named_payment_fault(
        fault_name: str,
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return _read_state(_parse_fault_name(fault_name))

    @app.put(
        "/internal/faults/{fault_name}",
        response_model=FaultStateResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
    )
    async def set_named_payment_fault(
        fault_name: str,
        update: FaultUpdateRequest,
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return _write_state(_parse_fault_name(fault_name), update.enabled)

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
