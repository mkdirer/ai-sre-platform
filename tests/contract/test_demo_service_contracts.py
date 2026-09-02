"""In-process API contract tests for the four Stage 01 services."""

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from apps.demo.gateway.main import create_app as create_gateway_app
from apps.demo.inventory_service.main import create_app as create_inventory_app
from apps.demo.order_service.main import create_app as create_order_app
from apps.demo.payment_service.main import create_app as create_payment_app
from packages.config import Settings
from packages.models.checkout import CheckoutResponse, PaymentResponse
from packages.models.http import ErrorResponse, HealthResponse
from tests.fakes import (
    RESERVATION_ID,
    FakeInventoryClient,
    FakeOrderClient,
    FakePaymentClient,
    FakePaymentStore,
)

pytestmark = pytest.mark.contract


def _settings() -> Settings:
    return Settings(_env_file=None, inventory_stock=5, inventory_unit_price_cents=1999)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://service.test",
    )


@pytest.mark.parametrize(
    "app,service_name",
    [
        (create_gateway_app(_settings(), order_client=FakeOrderClient(ready=False)), "gateway"),
        (
            create_order_app(
                _settings(),
                inventory_client=FakeInventoryClient(ready=False),
                payment_client=FakePaymentClient(ready=False),
            ),
            "order-service",
        ),
        (create_inventory_app(_settings()), "inventory-service"),
        (create_payment_app(_settings(), store=FakePaymentStore(ready=False)), "payment-service"),
    ],
)
@pytest.mark.asyncio
async def test_every_service_exposes_dependency_free_liveness(
    app: FastAPI,
    service_name: str,
) -> None:
    """Liveness describes only the process and never calls a dependency."""

    async with _client(app) as client:
        response = await client.get("/health/live")

    health = HealthResponse.model_validate(response.json())
    assert response.status_code == 200
    assert health.service == service_name
    assert health.dependencies == {}


@pytest.mark.asyncio
async def test_gateway_checkout_contract_and_correlation() -> None:
    """The public contract generates, echoes, and propagates one request ID."""

    order_client = FakeOrderClient()
    app = create_gateway_app(_settings(), order_client=order_client)
    async with _client(app) as client:
        response = await client.post(
            "/checkout",
            json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 2},
            headers={"Idempotency-Key": "contract-gateway-1"},
        )

    assert response.status_code == 200
    checkout = CheckoutResponse.model_validate(response.json())
    assert checkout.total_cents == 3998
    assert checkout.request_id == response.headers["X-Request-ID"]
    assert order_client.calls[0][1:] == ("contract-gateway-1", checkout.request_id)
    assert order_client.calls[0][0].customer_id == "customer-1"


@pytest.mark.asyncio
async def test_gateway_requires_safe_idempotency_key() -> None:
    """The public contract rejects absent or unsafe idempotency keys consistently."""

    app = create_gateway_app(_settings(), order_client=FakeOrderClient())
    async with _client(app) as client:
        missing = await client.post(
            "/checkout",
            json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 1},
        )
        invalid = await client.post(
            "/checkout",
            json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 1},
            headers={"Idempotency-Key": "contains spaces"},
        )

    assert missing.status_code == 400
    assert ErrorResponse.model_validate(missing.json()).code == "missing_idempotency_key"
    assert invalid.status_code == 400
    assert ErrorResponse.model_validate(invalid.json()).code == "invalid_idempotency_key"


@pytest.mark.asyncio
async def test_order_contract_calls_inventory_then_payment() -> None:
    """The order API converts reservation price data into the payment request."""

    inventory_client = FakeInventoryClient()
    payment_client = FakePaymentClient()
    app = create_order_app(
        _settings(),
        inventory_client=inventory_client,
        payment_client=payment_client,
    )
    request_id = "contract-order-request"
    async with _client(app) as client:
        response = await client.post(
            "/orders",
            json={
                "order_id": "14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
                "customer_id": "customer-2",
                "sku": "widget-001",
                "quantity": 2,
            },
            headers={
                "Idempotency-Key": "contract-order-1",
                "X-Request-ID": request_id,
            },
        )

    assert response.status_code == 200
    assert PaymentResponse.model_validate(response.json()).total_cents == 3998
    assert inventory_client.calls[0][1:] == ("contract-order-1", request_id)
    payment_request = payment_client.calls[0][0]
    assert payment_request.reservation_id == RESERVATION_ID
    assert payment_request.unit_price_cents == 1999
    assert payment_client.calls[0][1:] == ("contract-order-1", request_id)


@pytest.mark.asyncio
async def test_inventory_contract_has_dependency_free_readiness_and_typed_errors() -> None:
    """Inventory is ready without external checks and bounds its deterministic catalog."""

    app = create_inventory_app(_settings())
    async with _client(app) as client:
        ready_response = await client.get("/health/ready")
        unavailable_response = await client.post(
            "/reservations",
            json={
                "order_id": "14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
                "sku": "widget-001",
                "quantity": 6,
            },
            headers={"Idempotency-Key": "contract-inventory-1"},
        )

    ready = HealthResponse.model_validate(ready_response.json())
    assert ready_response.status_code == 200
    assert ready.dependencies == {}
    assert unavailable_response.status_code == 409
    assert ErrorResponse.model_validate(unavailable_response.json()).code == "insufficient_stock"


@pytest.mark.asyncio
async def test_payment_contract_persists_replays_and_reads_result() -> None:
    """The payment API exposes create/replay/read semantics through its store boundary."""

    store = FakePaymentStore()
    app = create_payment_app(_settings(), store=store)
    payload = {
        "order_id": "14af0742-8cf1-44d5-ab4b-a84ff94a8ea8",
        "reservation_id": "1c98b449-76e8-49ca-8e21-2ec33c7c8ac5",
        "customer_id": "customer-3",
        "sku": "widget-001",
        "quantity": 2,
        "unit_price_cents": 1999,
    }
    headers = {"Idempotency-Key": "contract-payment-1", "X-Request-ID": "payment-request"}
    async with _client(app) as client:
        first_response = await client.post("/payments", json=payload, headers=headers)
        replay_response = await client.post("/payments", json=payload, headers=headers)
        payment_id = PaymentResponse.model_validate(first_response.json()).payment_id
        read_response = await client.get(f"/payments/{payment_id}")

    first = PaymentResponse.model_validate(first_response.json())
    replay = PaymentResponse.model_validate(replay_response.json())
    read = PaymentResponse.model_validate(read_response.json())
    assert (
        first_response.status_code
        == replay_response.status_code
        == read_response.status_code
        == 200
    )
    assert replay.payment_id == first.payment_id
    assert replay.idempotent_replay is True
    assert read.payment_id == first.payment_id
    assert read.total_cents == 3998


@pytest.mark.asyncio
async def test_payment_fault_control_is_guarded_explicit_and_reversible() -> None:
    """The local control API rejects unauthenticated writes and reports exact state."""

    settings = Settings(
        _env_file=None,
        environment="test",
        telemetry_enabled=False,
        fault_injection_allowed=True,
        fault_control_token="contract-fault-token",
    )
    app = create_payment_app(settings, store=FakePaymentStore())
    control_headers = {"X-Fault-Control-Token": "contract-fault-token"}
    async with _client(app) as client:
        unauthorized = await client.put(
            "/internal/faults/slow-database",
            json={"enabled": True},
        )
        initial = await client.get(
            "/internal/faults/slow-database",
            headers=control_headers,
        )
        enabled = await client.put(
            "/internal/faults/slow-database",
            json={"enabled": True},
            headers=control_headers,
        )
        disabled = await client.put(
            "/internal/faults/slow-database",
            json={"enabled": False},
            headers=control_headers,
        )

    assert unauthorized.status_code == 401
    assert ErrorResponse.model_validate(unauthorized.json()).code == "fault_control_unauthorized"
    assert initial.json()["enabled"] is False
    assert enabled.json()["enabled"] is True
    assert enabled.json()["delay_seconds"] == 2.5
    assert disabled.json()["enabled"] is False


@pytest.mark.asyncio
async def test_readiness_checks_only_direct_dependencies() -> None:
    """Gateway and order readiness report their immediate required boundaries."""

    gateway_app = create_gateway_app(_settings(), order_client=FakeOrderClient(ready=False))
    order_app = create_order_app(
        _settings(),
        inventory_client=FakeInventoryClient(ready=True),
        payment_client=FakePaymentClient(ready=False),
    )
    payment_app = create_payment_app(_settings(), store=FakePaymentStore(ready=False))

    async with _client(gateway_app) as client:
        gateway_response = await client.get("/health/ready")
    async with _client(order_app) as client:
        order_response = await client.get("/health/ready")
    async with _client(payment_app) as client:
        payment_response = await client.get("/health/ready")

    assert gateway_response.status_code == 503
    assert ErrorResponse.model_validate(gateway_response.json()).code == "order_service_unavailable"
    assert order_response.status_code == 503
    assert ErrorResponse.model_validate(order_response.json()).code == "dependency_unavailable"
    assert payment_response.status_code == 503
    assert ErrorResponse.model_validate(payment_response.json()).code == "postgres_unavailable"


@pytest.mark.asyncio
async def test_invalid_correlation_id_is_rejected_with_a_safe_replacement() -> None:
    """Unsafe incoming correlation data is never propagated downstream."""

    order_client = FakeOrderClient()
    app = create_gateway_app(_settings(), order_client=order_client)
    async with _client(app) as client:
        response = await client.post(
            "/checkout",
            json={"customer_id": "customer-1", "sku": "widget-001", "quantity": 1},
            headers={
                "Idempotency-Key": "contract-request-id",
                "X-Request-ID": "unsafe request id",
            },
        )

    assert response.status_code == 400
    assert UUID(response.headers["X-Request-ID"])
    assert order_client.calls == []
