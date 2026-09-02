"""FastAPI request correlation and stable error handling."""

import inspect
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import Response as FastAPIResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from packages.config import Settings
from packages.models.checkout import IdempotencyKey
from packages.models.http import ErrorResponse
from packages.telemetry import (
    TelemetryRuntime,
    bind_request_id,
    current_trace_id,
    reset_request_id,
)
from packages.telemetry.metrics import CONTENT_TYPE_LATEST, normalized_route

REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
TRACE_ID_HEADER = "X-Trace-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ShutdownCallback = Callable[[], Awaitable[None] | None]


class ApiError(Exception):
    """Expected API failure rendered without leaking implementation detail."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def current_request_id(request: Request) -> str:
    """Return the request ID installed by middleware."""

    return cast(str, request.state.request_id)


async def require_idempotency_key(request: Request) -> IdempotencyKey:
    """Validate the public/internal idempotency header."""

    value = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if value is None:
        raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ApiError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1-128 safe ASCII characters",
        )
    return value


def create_service_app(
    *,
    title: str,
    service_name: str,
    settings: Settings,
) -> FastAPI:
    """Create a service app with lifecycle-owned correlation and telemetry."""

    telemetry = TelemetryRuntime.create(service_name=service_name, settings=settings)
    shutdown_callbacks: list[ShutdownCallback] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for callback in reversed(shutdown_callbacks):
                result = callback()
                if inspect.isawaitable(result):
                    await result
            telemetry.shutdown()

    app = FastAPI(title=title, version=settings.service_version, lifespan=lifespan)
    app.state.telemetry = telemetry
    app.state.shutdown_callbacks = shutdown_callbacks

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> FastAPIResponse:
        return FastAPIResponse(content=telemetry.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        started_at = time.perf_counter()
        telemetry.metrics.begin(request.method)
        supplied = request.headers.get(REQUEST_ID_HEADER)
        if supplied is not None and _REQUEST_ID_PATTERN.fullmatch(supplied) is None:
            request_id = str(uuid4())
            response: Response = _error_response(
                status_code=400,
                code="invalid_request_id",
                message="X-Request-ID must contain 1-64 safe ASCII characters",
                request_id=request_id,
            )
        else:
            request_id = supplied or str(uuid4())
            response = Response(status_code=500)

        request.state.request_id = request_id
        request_id_token = bind_request_id(request_id)
        response_status = response.status_code
        error_type: str | None = None
        try:
            if supplied is None or _REQUEST_ID_PATTERN.fullmatch(supplied) is not None:
                response = await call_next(request)
                response_status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            trace_id = current_trace_id()
            if trace_id is not None:
                response.headers[TRACE_ID_HEADER] = trace_id
            return response
        except Exception as error:
            response_status = 500
            error_type = type(error).__name__
            raise
        finally:
            duration_seconds = time.perf_counter() - started_at
            route = normalized_route(request.scope)
            telemetry.metrics.finish(
                method=request.method,
                route=route,
                response_status=response_status,
                duration_seconds=duration_seconds,
            )
            if request.url.path not in {"/health/live", "/health/ready", "/metrics"}:
                attributes: dict[str, object] = {
                    "http.method": request.method,
                    "http.route": route,
                    "http.status_code": response_status,
                    "duration_ms": round(duration_seconds * 1_000, 3),
                }
                if error_type is not None:
                    attributes["error.type"] = error_type
                telemetry.logger.info("http.request.completed", extra={"structured": attributes})
            reset_request_id(request_id_token)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            request_id=current_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            request_id=current_request_id(request),
        )

    telemetry.instrument_fastapi(app)
    return app


def get_telemetry(app: FastAPI) -> TelemetryRuntime:
    """Return the typed telemetry runtime owned by a service app."""

    return cast(TelemetryRuntime, app.state.telemetry)


def register_shutdown_callback(app: FastAPI, callback: ShutdownCallback) -> None:
    """Register a service-owned cleanup operation with the shared lifespan."""

    callbacks = cast(list[ShutdownCallback], app.state.shutdown_callbacks)
    callbacks.append(callback)


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message, request_id=request_id)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
