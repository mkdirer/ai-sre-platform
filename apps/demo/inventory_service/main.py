"""Deterministic inventory reservation service."""

from typing import Annotated
from uuid import UUID, uuid5

from fastapi import Depends, FastAPI, Header, Request

from apps.demo.common.faults import (
    FaultControlDisabledError,
    FaultControlUnauthorizedError,
    MultiFaultController,
)
from apps.demo.common.web import (
    ApiError,
    create_service_app,
    current_request_id,
    get_telemetry,
    require_idempotency_key,
)
from packages.config import Settings
from packages.models.checkout import (
    IdempotencyKey,
    ReservationRequest,
    ReservationResponse,
)
from packages.models.faults import (
    INVENTORY_FAULTS,
    FaultListResponse,
    FaultName,
    FaultStateResponse,
    FaultUpdateRequest,
)
from packages.models.http import ErrorResponse, HealthResponse

_RESERVATION_ID_NAMESPACE = UUID("a0447bf5-1d52-45c9-86fb-bce41dbe991c")


def _parse_inventory_fault(raw: str) -> FaultName:
    """Parse a URL fault segment to the inventory allowlist."""

    normalized = raw.replace("-", "_")
    try:
        fault = FaultName(normalized)
    except ValueError as error:
        raise ApiError(404, "fault_not_found", f"unknown fault: {raw}") from error
    if fault not in INVENTORY_FAULTS:
        raise ApiError(404, "fault_not_found", f"fault not owned by inventory-service: {raw}")
    return fault


def create_app(
    settings: Settings | None = None,
    *,
    multi_fault_controller: MultiFaultController | None = None,
) -> FastAPI:
    """Build the dependency-free inventory service."""

    resolved_settings = settings or Settings()
    app = create_service_app(
        title="AI SRE Demo Inventory Service",
        service_name="inventory-service",
        settings=resolved_settings,
    )
    telemetry = get_telemetry(app)

    def _inventory_state_callback(fault: FaultName, enabled: bool) -> None:
        telemetry.metrics.set_fault_enabled(fault.value, enabled)

    resolved_faults = multi_fault_controller or MultiFaultController.from_settings(
        resolved_settings,
        service_name="inventory-service",
        faults=INVENTORY_FAULTS,
        telemetry=telemetry,
        state_callback=_inventory_state_callback,
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
        # Bounded simulated upstream timeout for Stage 09 evals.
        await resolved_faults.inject_delay(FaultName.INVENTORY_TIMEOUT)
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

    def authorize_fault_control(token: str | None) -> None:
        try:
            resolved_faults.authorize(token)
        except FaultControlDisabledError as error:
            raise ApiError(
                403,
                "fault_control_disabled",
                "Fault control is disabled outside an explicitly allowed local environment",
            ) from error
        except FaultControlUnauthorizedError as error:
            raise ApiError(
                401,
                "fault_control_unauthorized",
                "A valid X-Fault-Control-Token header is required",
            ) from error

    @app.get(
        "/internal/faults",
        response_model=FaultListResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def list_inventory_faults(
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultListResponse:
        authorize_fault_control(x_fault_control_token)
        return FaultListResponse(faults=resolved_faults.states())

    @app.get(
        "/internal/faults/{fault_name}",
        response_model=FaultStateResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
    )
    async def get_inventory_fault(
        fault_name: str,
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return resolved_faults.state(_parse_inventory_fault(fault_name))

    @app.put(
        "/internal/faults/{fault_name}",
        response_model=FaultStateResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
    )
    async def set_inventory_fault(
        fault_name: str,
        update: FaultUpdateRequest,
        x_fault_control_token: Annotated[
            str | None,
            Header(alias="X-Fault-Control-Token"),
        ] = None,
    ) -> FaultStateResponse:
        authorize_fault_control(x_fault_control_token)
        return resolved_faults.set_enabled(_parse_inventory_fault(fault_name), update.enabled)

    return app
