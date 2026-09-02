"""Bounded Milestone 1C slow-database and alert delivery scenario."""

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import httpx
from pydantic import ValidationError

from packages.models.alerts import AlertDeliveryList
from packages.models.checkout import CheckoutResponse
from packages.models.faults import FaultStateResponse

ALERT_NAME = "DemoPaymentHighLatency"
PAYMENT_P95_QUERY = (
    "histogram_quantile(0.95, sum by (le) "
    '(rate(demo_http_request_duration_seconds_bucket{service="payment-service",'
    'method="POST",route="/payments"}[20s])))'
)
FAULT_GAUGE_QUERY = 'demo_fault_enabled{service="payment-service",fault="slow_database"}'
TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Arguments:
    """Validated local endpoints and fixed scenario bounds."""

    gateway_url: str
    payment_url: str
    prometheus_url: str
    loki_url: str
    tempo_url: str
    alertmanager_url: str
    receiver_url: str
    fault_control_token: str
    traffic_count: int
    request_timeout_seconds: float
    poll_deadline_seconds: float


@dataclass(frozen=True)
class CheckoutEvidence:
    """Correlation metadata and measured latency for one scenario request."""

    request_id: str
    trace_id: str
    started_at_ns: int
    latency_seconds: float


def _bounded_count(value: str) -> int:
    count = int(value)
    if not 4 <= count <= 12:
        raise argparse.ArgumentTypeError("traffic count must be between 4 and 12")
    return count


def _bounded_seconds(value: str) -> float:
    seconds = float(value)
    if not 1 <= seconds <= 180:
        raise argparse.ArgumentTypeError("duration must be between 1 and 180 seconds")
    return seconds


def parse_arguments() -> Arguments:
    """Parse scenario-specific environment variables and CLI overrides."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("SMOKE_GATEWAY_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument(
        "--payment-url",
        default=os.getenv("SMOKE_PAYMENT_URL", "http://127.0.0.1:8004"),
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.getenv("SMOKE_PROMETHEUS_URL", "http://127.0.0.1:9090"),
    )
    parser.add_argument(
        "--loki-url",
        default=os.getenv("SMOKE_LOKI_URL", "http://127.0.0.1:3100"),
    )
    parser.add_argument(
        "--tempo-url",
        default=os.getenv("SMOKE_TEMPO_URL", "http://127.0.0.1:3200"),
    )
    parser.add_argument(
        "--alertmanager-url",
        default=os.getenv("SCENARIO_ALERTMANAGER_URL", "http://127.0.0.1:9093"),
    )
    parser.add_argument(
        "--receiver-url",
        default=os.getenv("SCENARIO_ALERT_RECEIVER_URL", "http://127.0.0.1:8005"),
    )
    parser.add_argument(
        "--fault-control-token",
        default=os.getenv(
            "SCENARIO_FAULT_CONTROL_TOKEN",
            os.getenv("FAULT_CONTROL_TOKEN", "local-demo-fault-control"),
        ),
    )
    parser.add_argument(
        "--traffic-count",
        type=_bounded_count,
        default=_bounded_count(os.getenv("SCENARIO_TRAFFIC_COUNT", "8")),
    )
    parser.add_argument("--request-timeout", type=_bounded_seconds, default=10.0)
    parser.add_argument("--poll-deadline", type=_bounded_seconds, default=60.0)
    parsed = parser.parse_args()
    token = str(parsed.fault_control_token)
    if not token:
        parser.error("fault control token must not be empty")
    return Arguments(
        gateway_url=str(parsed.gateway_url).rstrip("/"),
        payment_url=str(parsed.payment_url).rstrip("/"),
        prometheus_url=str(parsed.prometheus_url).rstrip("/"),
        loki_url=str(parsed.loki_url).rstrip("/"),
        tempo_url=str(parsed.tempo_url).rstrip("/"),
        alertmanager_url=str(parsed.alertmanager_url).rstrip("/"),
        receiver_url=str(parsed.receiver_url).rstrip("/"),
        fault_control_token=token,
        traffic_count=int(parsed.traffic_count),
        request_timeout_seconds=float(parsed.request_timeout),
        poll_deadline_seconds=float(parsed.poll_deadline),
    )


def _require_success(response: httpx.Response, operation: str) -> None:
    if response.is_error:
        raise RuntimeError(f"{operation} failed with HTTP {response.status_code}: {response.text}")


async def _wait_for[T](
    description: str,
    *,
    deadline_seconds: float,
    probe: Callable[[], Awaitable[T | None]],
) -> T:
    deadline = time.monotonic() + deadline_seconds
    last_diagnostic = "condition not met"
    while time.monotonic() < deadline:
        try:
            result = await probe()
            if result is not None:
                return result
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            last_diagnostic = f"{type(error).__name__}: {error}"
        await asyncio.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {description}: {last_diagnostic}")


async def _wait_for_http_ready(
    client: httpx.AsyncClient,
    *,
    name: str,
    url: str,
    deadline_seconds: float,
) -> None:
    async def probe() -> bool | None:
        response = await client.get(url)
        if response.status_code != 200:
            raise ValueError(f"{name} returned HTTP {response.status_code}: {response.text[:160]}")
        return True

    await _wait_for(name, deadline_seconds=deadline_seconds, probe=probe)


async def _set_fault(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    enabled: bool,
) -> FaultStateResponse:
    response = await client.put(
        f"{arguments.payment_url}/internal/faults/slow-database",
        json={"enabled": enabled},
        headers={"X-Fault-Control-Token": arguments.fault_control_token},
    )
    _require_success(response, f"set slow_database enabled={enabled}")
    state = FaultStateResponse.model_validate(response.json())
    if state.enabled is not enabled:
        raise RuntimeError("payment service did not apply the requested fault state")
    print(
        f"fault enabled={str(state.enabled).lower()} allowed={str(state.allowed).lower()} "
        f"delay_seconds={state.delay_seconds} service={state.service} "
        f"version={state.service_version} environment={state.environment}"
    )
    return state


@asynccontextmanager
async def enabled_fault(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> AsyncIterator[FaultStateResponse]:
    """Enable explicitly and guarantee a disable attempt on every exit path."""

    state = await _set_fault(client, arguments, enabled=True)
    try:
        yield state
    finally:
        await _set_fault(client, arguments, enabled=False)


async def _checkout(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    label: str,
) -> CheckoutEvidence:
    suffix = uuid4().hex
    request_id = f"stage03-{label}-{suffix}"
    started_at_ns = time.time_ns() - 1_000_000_000
    started_at = time.perf_counter()
    response = await client.post(
        f"{arguments.gateway_url}/checkout",
        json={"customer_id": "stage03-customer", "sku": "widget-001", "quantity": 1},
        headers={
            "Idempotency-Key": f"stage03-{label}-{suffix}",
            "X-Request-ID": request_id,
        },
    )
    elapsed = time.perf_counter() - started_at
    _require_success(response, f"{label} checkout")
    checkout = CheckoutResponse.model_validate(response.json())
    trace_id = response.headers.get("X-Trace-ID", "")
    if checkout.request_id != request_id or response.headers.get("X-Request-ID") != request_id:
        raise RuntimeError("checkout did not preserve the scenario request ID")
    if TRACE_ID_PATTERN.fullmatch(trace_id) is None:
        raise RuntimeError("checkout did not return a valid trace ID")
    print(
        f"checkout phase={label} request_id={request_id} "
        f"trace_id={trace_id} "
        f"latency_seconds={elapsed:.3f} payment_id={checkout.payment_id}"
    )
    return CheckoutEvidence(
        request_id=request_id,
        trace_id=trace_id,
        started_at_ns=started_at_ns,
        latency_seconds=elapsed,
    )


def _otel_attributes(attributes: object) -> dict[str, object]:
    if not isinstance(attributes, list):
        return {}
    values: dict[str, object] = {}
    for attribute in attributes:
        if not isinstance(attribute, dict) or not isinstance(attribute.get("key"), str):
            continue
        value = attribute.get("value")
        if not isinstance(value, dict):
            continue
        for value_key in ("stringValue", "boolValue", "doubleValue", "intValue"):
            if value_key in value:
                values[attribute["key"]] = value[value_key]
                break
    return values


def _fault_log_summary(payload: object, trace_id: str) -> str | None:
    """Validate the JSON formatter's actual `event` schema for one fault log."""

    if not isinstance(payload, dict):
        return None
    attributes = payload.get("attributes", {})
    if not isinstance(attributes, dict):
        return None
    if not (
        payload.get("event") == "fault.slow_database.injected"
        and payload.get("service") == "payment-service"
        and payload.get("service.version")
        and payload.get("deployment.environment")
        and attributes.get("fault.enabled") is True
        and attributes.get("fault.name") == "slow_database"
    ):
        return None
    return (
        f"trace_id={trace_id} service={payload['service']} "
        f"version={payload['service.version']} "
        f"environment={payload['deployment.environment']} fault.enabled=true"
    )


async def _prove_fault_log(
    client: httpx.AsyncClient,
    arguments: Arguments,
    evidence: CheckoutEvidence,
) -> str:
    async def probe() -> str | None:
        response = await client.get(
            f"{arguments.loki_url}/loki/api/v1/query_range",
            params={
                "query": (
                    '{service_name="payment-service"} '
                    f'|= "{evidence.trace_id}" |= "fault.slow_database.injected"'
                ),
                "start": str(evidence.started_at_ns),
                "end": str(time.time_ns()),
                "limit": "20",
                "direction": "backward",
            },
        )
        _require_success(response, "Loki fault log query")
        for result in response.json().get("data", {}).get("result", []):
            for _timestamp, line in result.get("values", []):
                payload = json.loads(line)
                summary = _fault_log_summary(payload, evidence.trace_id)
                if summary is not None:
                    return summary
        raise ValueError(f"no matching fault log for trace_id={evidence.trace_id}")

    return await _wait_for(
        "fault-enriched Loki log",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _prove_fault_trace(
    client: httpx.AsyncClient,
    arguments: Arguments,
    evidence: CheckoutEvidence,
) -> str:
    async def probe() -> str | None:
        response = await client.get(f"{arguments.tempo_url}/api/traces/{evidence.trace_id}")
        if response.status_code == 404:
            raise ValueError(f"Tempo has not ingested trace_id={evidence.trace_id}")
        _require_success(response, "Tempo fault trace query")
        matching_attributes: dict[str, object] | None = None

        def visit(node: object) -> None:
            nonlocal matching_attributes
            if matching_attributes is not None:
                return
            if isinstance(node, dict):
                if "spanId" in node and "traceId" in node:
                    attributes = _otel_attributes(node.get("attributes"))
                    if (
                        attributes.get("fault.name") == "slow_database"
                        and attributes.get("fault.enabled") is True
                        and attributes.get("service.version")
                        and attributes.get("deployment.environment")
                    ):
                        matching_attributes = attributes
                        return
                for nested in node.values():
                    visit(nested)
            elif isinstance(node, list):
                for nested in node:
                    visit(nested)

        visit(response.json())
        if matching_attributes is None:
            raise ValueError(f"trace_id={evidence.trace_id} has no fault-enriched payment span yet")
        return (
            f"trace_id={evidence.trace_id} fault.enabled=true "
            f"version={matching_attributes['service.version']} "
            f"environment={matching_attributes['deployment.environment']}"
        )

    return await _wait_for(
        "fault-enriched Tempo span",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _prometheus_query(
    client: httpx.AsyncClient,
    arguments: Arguments,
    query: str,
) -> list[dict[str, object]]:
    response = await client.get(
        f"{arguments.prometheus_url}/api/v1/query",
        params={"query": query},
    )
    _require_success(response, "Prometheus query")
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query returned an invalid payload: {payload}")
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        raise RuntimeError("Prometheus query result is not a list")
    return result


async def _wait_for_metric_value(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    query: str,
    predicate: Callable[[float], bool],
    description: str,
) -> float:
    async def probe() -> float | None:
        results = await _prometheus_query(client, arguments, query)
        if not results:
            raise ValueError("Prometheus query returned no series")
        value = float(results[0]["value"][1])  # type: ignore[index]
        if not predicate(value):
            raise ValueError(f"Prometheus observed value={value}")
        return value

    return await _wait_for(
        description,
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _prometheus_alert_is_firing(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> bool:
    response = await client.get(f"{arguments.prometheus_url}/api/v1/alerts")
    _require_success(response, "Prometheus alerts query")
    alerts = response.json().get("data", {}).get("alerts", [])
    return any(
        alert.get("labels", {}).get("alertname") == ALERT_NAME and alert.get("state") == "firing"
        for alert in alerts
    )


async def _wait_for_firing_alert(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> bool:
    async def probe() -> bool | None:
        if not await _prometheus_alert_is_firing(client, arguments):
            raise ValueError(f"{ALERT_NAME} is not firing")
        return True

    return await _wait_for(
        "Prometheus firing alert",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _wait_for_alert_recovery(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> bool:
    async def probe() -> bool | None:
        if await _prometheus_alert_is_firing(client, arguments):
            raise ValueError(f"{ALERT_NAME} is still firing")
        return True

    return await _wait_for(
        "Prometheus alert recovery",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _wait_for_alertmanager_alert(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> bool:
    async def probe() -> bool | None:
        response = await client.get(f"{arguments.alertmanager_url}/api/v2/alerts")
        _require_success(response, "Alertmanager alerts query")
        alerts = response.json()
        found = any(
            alert.get("labels", {}).get("alertname") == ALERT_NAME
            and alert.get("status", {}).get("state") == "active"
            for alert in alerts
        )
        if not found:
            raise ValueError(f"Alertmanager has no active {ALERT_NAME}")
        return True

    return await _wait_for(
        "Alertmanager active alert",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _wait_for_delivery(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    delivery_status: str,
) -> int:
    async def probe() -> int | None:
        response = await client.get(
            f"{arguments.receiver_url}/deliveries",
            params={"alertname": ALERT_NAME, "status": delivery_status},
        )
        _require_success(response, "alert receiver delivery query")
        deliveries = AlertDeliveryList.model_validate(response.json()).deliveries
        if not deliveries:
            raise ValueError(f"receiver has no {delivery_status} {ALERT_NAME} delivery")
        return deliveries[-1].sequence

    return await _wait_for(
        f"{delivery_status} webhook delivery",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def run_scenario(arguments: Arguments) -> None:
    """Prove baseline, deterministic degradation, alert delivery, and recovery."""

    timeout = httpx.Timeout(arguments.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        readiness_checks = (
            ("gateway readiness", f"{arguments.gateway_url}/health/ready"),
            ("payment readiness", f"{arguments.payment_url}/health/ready"),
            ("Prometheus readiness", f"{arguments.prometheus_url}/-/ready"),
            ("Loki readiness", f"{arguments.loki_url}/ready"),
            ("Tempo readiness", f"{arguments.tempo_url}/ready"),
            ("Alertmanager readiness", f"{arguments.alertmanager_url}/-/ready"),
            ("alert receiver readiness", f"{arguments.receiver_url}/health/ready"),
        )
        for name, url in readiness_checks:
            await _wait_for_http_ready(
                client,
                name=name,
                url=url,
                deadline_seconds=arguments.poll_deadline_seconds,
            )

        await _set_fault(client, arguments, enabled=False)
        await _wait_for_alert_recovery(client, arguments)
        clear_response = await client.delete(f"{arguments.receiver_url}/deliveries")
        _require_success(clear_response, "clear alert receiver")

        baseline = await _checkout(client, arguments, label="baseline")
        if baseline.latency_seconds >= 1.0:
            raise RuntimeError(
                f"normal checkout was unexpectedly slow: {baseline.latency_seconds:.3f} seconds"
            )

        async with enabled_fault(client, arguments) as state:
            if not 2.0 <= state.delay_seconds <= 3.0:
                raise RuntimeError("configured slow_database delay is outside 2-3 seconds")
            gauge_value = await _wait_for_metric_value(
                client,
                arguments,
                query=FAULT_GAUGE_QUERY,
                predicate=lambda value: value == 1.0,
                description="enabled fault gauge",
            )
            print(f"prometheus fault_gauge={gauge_value:.0f}")

            slow_checkouts = [
                await _checkout(client, arguments, label=f"fault-{index + 1}")
                for index in range(arguments.traffic_count)
            ]
            slow_latencies = [checkout.latency_seconds for checkout in slow_checkouts]
            median_latency = statistics.median(slow_latencies)
            if not 2.0 <= median_latency <= 3.5:
                raise RuntimeError(
                    f"fault median latency was outside the expected range: {median_latency:.3f}"
                )
            print(
                f"latency baseline={baseline.latency_seconds:.3f} "
                f"fault_min={min(slow_latencies):.3f} "
                f"fault_median={median_latency:.3f} "
                f"fault_max={max(slow_latencies):.3f}"
            )
            log_evidence = await _prove_fault_log(client, arguments, slow_checkouts[0])
            trace_evidence = await _prove_fault_trace(client, arguments, slow_checkouts[0])
            print(f"fault_log verified=true evidence={log_evidence}")
            print(f"fault_trace verified=true evidence={trace_evidence}")

            p95 = await _wait_for_metric_value(
                client,
                arguments,
                query=PAYMENT_P95_QUERY,
                predicate=lambda value: value > 2.0,
                description="payment p95 above 2 seconds",
            )
            await _wait_for_firing_alert(client, arguments)
            await _wait_for_alertmanager_alert(client, arguments)
            firing_sequence = await _wait_for_delivery(
                client,
                arguments,
                delivery_status="firing",
            )
            print(
                f"alert name={ALERT_NAME} prometheus=firing alertmanager=active "
                f"p95_seconds={p95:.3f} webhook_sequence={firing_sequence}"
            )

        recovered = await _checkout(client, arguments, label="recovery")
        if recovered.latency_seconds >= 1.0:
            raise RuntimeError(
                "checkout did not recover after disabling the fault: "
                f"{recovered.latency_seconds:.3f}"
            )
        gauge_value = await _wait_for_metric_value(
            client,
            arguments,
            query=FAULT_GAUGE_QUERY,
            predicate=lambda value: value == 0.0,
            description="disabled fault gauge",
        )
        await _wait_for_alert_recovery(client, arguments)
        resolved_sequence = await _wait_for_delivery(
            client,
            arguments,
            delivery_status="resolved",
        )
        print(
            f"recovery fault_gauge={gauge_value:.0f} "
            f"latency_seconds={recovered.latency_seconds:.3f} "
            f"prometheus=inactive webhook_status=resolved webhook_sequence={resolved_sequence}"
        )


def main() -> int:
    """Run the bounded scenario and return a shell-friendly status."""

    try:
        asyncio.run(run_scenario(parse_arguments()))
    except (
        argparse.ArgumentError,
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValidationError,
        ValueError,
    ) as error:
        print(f"slow_database scenario failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
