"""Unit tests for bounded service-to-service HTTP behavior."""

from collections.abc import Callable

import httpx
import pytest

from apps.demo.common.http_client import ServiceCallError, ServiceHttpClient
from apps.demo.common.web import REQUEST_ID_HEADER
from packages.models.checkout import ReservationRequest, ReservationResponse


def _reservation_response(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "reservation_id": "1c98b449-76e8-49ca-8e21-2ec33c7c8ac5",
        "order_id": "14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
        "sku": "widget-001",
        "quantity": 2,
        "unit_price_cents": 1999,
        "status": "reserved",
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ServiceHttpClient:
    return ServiceHttpClient(
        service_name="inventory_service",
        base_url="http://inventory.test",
        timeout_seconds=0.1,
        max_attempts=2,
        retry_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_post_retries_server_failure_and_preserves_headers() -> None:
    """A retry-safe POST is retried once with the same correlation and idempotency values."""

    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json=_reservation_response("request-123"),
            headers={REQUEST_ID_HEADER: "request-123"},
            request=request,
        )

    response = await _client(handler).post_model(
        path="/reservations",
        payload=ReservationRequest(
            order_id="14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
            sku="widget-001",
            quantity=2,
        ),
        response_model=ReservationResponse,
        idempotency_key="checkout-123",
        request_id="request-123",
    )

    assert response.request_id == "request-123"
    assert len(attempts) == 2
    assert all(request.headers["Idempotency-Key"] == "checkout-123" for request in attempts)
    assert all(request.headers["X-Request-ID"] == "request-123" for request in attempts)


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_mapped() -> None:
    """Exhausted attempts become a stable timeout without leaking the client exception."""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("test timeout", request=request)

    with pytest.raises(ServiceCallError) as error_info:
        await _client(handler).post_model(
            path="/reservations",
            payload=ReservationRequest(
                order_id="14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
                sku="widget-001",
                quantity=2,
            ),
            response_model=ReservationResponse,
            idempotency_key="checkout-timeout",
            request_id="request-timeout",
        )

    assert attempts == 2
    assert error_info.value.status_code == 504
    assert error_info.value.code == "inventory_service_timeout"


@pytest.mark.asyncio
async def test_response_with_wrong_request_id_is_rejected() -> None:
    """A downstream service cannot silently break request correlation."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_reservation_response("different-request"),
            headers={REQUEST_ID_HEADER: "different-request"},
            request=request,
        )

    with pytest.raises(ServiceCallError) as error_info:
        await _client(handler).post_model(
            path="/reservations",
            payload=ReservationRequest(
                order_id="14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
                sku="widget-001",
                quantity=2,
            ),
            response_model=ReservationResponse,
            idempotency_key="checkout-correlation",
            request_id="expected-request",
        )

    assert error_info.value.status_code == 502
    assert error_info.value.code == "inventory_service_invalid_response"


@pytest.mark.asyncio
async def test_typed_upstream_business_error_is_mapped() -> None:
    """Safe business errors preserve status while gaining the upstream service namespace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "code": "insufficient_stock",
                "message": "requested quantity is not available",
                "request_id": "request-stock",
            },
            request=request,
        )

    with pytest.raises(ServiceCallError) as error_info:
        await _client(handler).post_model(
            path="/reservations",
            payload=ReservationRequest(
                order_id="14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
                sku="widget-001",
                quantity=100,
            ),
            response_model=ReservationResponse,
            idempotency_key="checkout-stock",
            request_id="request-stock",
        )

    assert error_info.value.status_code == 409
    assert error_info.value.code == "inventory_service_insufficient_stock"
    assert error_info.value.message == "requested quantity is not available"
