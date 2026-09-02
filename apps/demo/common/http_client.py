"""Bounded HTTP client used for service-to-service calls."""

import asyncio
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from apps.demo.common.web import IDEMPOTENCY_KEY_HEADER, REQUEST_ID_HEADER
from packages.models.http import ErrorResponse
from packages.telemetry import TelemetryRuntime, inject_trace_context

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class ServiceCallError(Exception):
    """Normalized upstream failure for deterministic API error mapping."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ServiceHttpClient:
    """Small retrying JSON client with fixed base URL and bounded attempts."""

    def __init__(
        self,
        *,
        service_name: str,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        retry_backoff_seconds: float,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._service_name = service_name
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._telemetry = telemetry
        self._transport = transport

    async def post_model(
        self,
        *,
        path: str,
        payload: BaseModel,
        response_model: type[ResponseModelT],
        idempotency_key: str,
        request_id: str,
    ) -> ResponseModelT:
        """POST a typed payload and validate the typed response."""

        headers = {
            IDEMPOTENCY_KEY_HEADER: idempotency_key,
            REQUEST_ID_HEADER: request_id,
        }
        inject_trace_context(headers)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            if self._telemetry is not None:
                self._telemetry.instrument_httpx_client(client)
            for attempt in range(1, self._max_attempts + 1):
                try:
                    response = await client.post(
                        path,
                        json=payload.model_dump(mode="json"),
                        headers=headers,
                    )
                except httpx.TimeoutException as error:
                    if attempt < self._max_attempts:
                        await self._backoff(attempt)
                        continue
                    raise ServiceCallError(
                        504,
                        f"{self._service_name}_timeout",
                        f"{self._service_name} timed out",
                    ) from error
                except httpx.RequestError as error:
                    if attempt < self._max_attempts:
                        await self._backoff(attempt)
                        continue
                    raise ServiceCallError(
                        503,
                        f"{self._service_name}_unavailable",
                        f"{self._service_name} is unavailable",
                    ) from error

                if response.status_code >= 500 and attempt < self._max_attempts:
                    await self._backoff(attempt)
                    continue
                return self._parse_response(
                    response=response,
                    response_model=response_model,
                    request_id=request_id,
                )

        raise RuntimeError("bounded HTTP attempt loop exited unexpectedly")

    async def is_ready(self) -> bool:
        """Return readiness without propagating network or validation errors."""

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                if self._telemetry is not None:
                    self._telemetry.instrument_httpx_client(client)
                response = await client.get("/health/ready")
        except httpx.RequestError:
            return False
        return response.status_code == 200

    def _parse_response(
        self,
        *,
        response: httpx.Response,
        response_model: type[ResponseModelT],
        request_id: str,
    ) -> ResponseModelT:
        if response.is_error:
            self._raise_upstream_error(response)

        if response.headers.get(REQUEST_ID_HEADER) != request_id:
            raise ServiceCallError(
                502,
                f"{self._service_name}_invalid_response",
                f"{self._service_name} did not preserve the request ID",
            )

        try:
            return response_model.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise ServiceCallError(
                502,
                f"{self._service_name}_invalid_response",
                f"{self._service_name} returned an invalid response",
            ) from error

    def _raise_upstream_error(self, response: httpx.Response) -> None:
        try:
            error_response = ErrorResponse.model_validate(response.json())
            code = f"{self._service_name}_{error_response.code}"
            message = error_response.message
        except (ValueError, ValidationError):
            code = f"{self._service_name}_failure"
            message = f"{self._service_name} returned HTTP {response.status_code}"

        mapped_status = response.status_code if response.status_code in {400, 409, 422} else 502
        raise ServiceCallError(mapped_status, code, message)

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * attempt
        if delay > 0:
            await asyncio.sleep(delay)
