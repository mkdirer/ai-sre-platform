"""Shared bounded JSON transport used only by fixed telemetry clients."""

import asyncio
import json
from collections.abc import Mapping

import httpx

from packages.telemetry import TelemetryRuntime

QueryValue = str | int | float


class AdapterError(Exception):
    """Base class for deterministic telemetry adapter failures."""


class AdapterTimeoutError(AdapterError):
    """A backend did not answer within its configured request deadline."""


class AdapterUnavailableError(AdapterError):
    """A backend could not be reached or returned a retryable server failure."""


class AdapterResponseError(AdapterError):
    """A backend returned malformed, oversized, or semantically invalid data."""


class AdapterQueryError(AdapterError):
    """A fixed repository-owned query was rejected by its backend."""


class BoundedJsonClient:
    """Lifecycle-owned retrying GET transport with no public generic request method."""

    def __init__(
        self,
        *,
        source: str,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        retry_backoff_seconds: float,
        max_response_bytes: int,
        telemetry: TelemetryRuntime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._source = source
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )
        if telemetry is not None:
            telemetry.instrument_httpx_client(self._client)

    async def _get_json(
        self,
        *,
        path: str,
        params: Mapping[str, QueryValue],
        not_found_is_empty: bool = False,
    ) -> dict[str, object] | None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._client.stream(
                    "GET",
                    path,
                    params=params,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code == 404 and not_found_is_empty:
                        return None
                    if response.status_code >= 500:
                        if attempt < self._max_attempts:
                            await self._backoff(attempt)
                            continue
                        raise AdapterUnavailableError(
                            f"{self._source} returned HTTP {response.status_code}"
                        )
                    if response.is_error:
                        raise AdapterQueryError(
                            f"{self._source} rejected a fixed query with HTTP "
                            f"{response.status_code}"
                        )
                    content_encoding = response.headers.get("content-encoding", "identity")
                    if content_encoding.casefold() != "identity":
                        raise AdapterResponseError(
                            f"{self._source} returned an unsupported compressed response"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as error:
                            raise AdapterResponseError(
                                f"{self._source} returned an invalid content length"
                            ) from error
                        if declared_length > self._max_response_bytes:
                            raise AdapterResponseError(
                                f"{self._source} response exceeded the size limit"
                            )
                    content = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(content) + len(chunk) > self._max_response_bytes:
                            raise AdapterResponseError(
                                f"{self._source} response exceeded the size limit"
                            )
                        content.extend(chunk)
            except httpx.TimeoutException as error:
                if attempt < self._max_attempts:
                    await self._backoff(attempt)
                    continue
                raise AdapterTimeoutError(f"{self._source} request timed out") from error
            except httpx.RequestError as error:
                if attempt < self._max_attempts:
                    await self._backoff(attempt)
                    continue
                raise AdapterUnavailableError(f"{self._source} is unavailable") from error
            try:
                payload = json.loads(content)
            except (UnicodeDecodeError, ValueError) as error:
                raise AdapterResponseError(f"{self._source} returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise AdapterResponseError(f"{self._source} returned a non-object response")
            return payload
        raise RuntimeError("bounded telemetry HTTP attempt loop exited unexpectedly")

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * attempt
        if delay > 0:
            await asyncio.sleep(delay)

    async def close(self) -> None:
        """Release the owned HTTP connection pool."""

        await self._client.aclose()
