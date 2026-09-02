"""FastAPI request correlation and stable error handling."""

import re
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from packages.models.checkout import IdempotencyKey
from packages.models.http import ErrorResponse

REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


def create_service_app(*, title: str) -> FastAPI:
    """Create a service app with consistent correlation and error envelopes."""

    app = FastAPI(title=title, version="0.1.0")

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER)
        if supplied is not None and _REQUEST_ID_PATTERN.fullmatch(supplied) is None:
            request_id = str(uuid4())
            invalid_response = _error_response(
                status_code=400,
                code="invalid_request_id",
                message="X-Request-ID must contain 1-64 safe ASCII characters",
                request_id=request_id,
            )
            invalid_response.headers[REQUEST_ID_HEADER] = request_id
            return invalid_response

        request_id = supplied or str(uuid4())
        request.state.request_id = request_id
        downstream_response = await call_next(request)
        downstream_response.headers[REQUEST_ID_HEADER] = request_id
        return downstream_response

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

    return app


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message, request_id=request_id)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
