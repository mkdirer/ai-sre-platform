"""Bounded proof that one checkout correlates across Prometheus, Loki, and Tempo."""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from packages.models.checkout import CheckoutResponse

_EXPECTED_ROUTES = {
    "gateway": "/checkout",
    "order-service": "/orders",
    "inventory-service": "/reservations",
    "payment-service": "/payments",
}
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_PollCheck = Callable[[], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class Arguments:
    """Validated smoke endpoints and deadlines."""

    gateway_url: str
    prometheus_url: str
    loki_url: str
    tempo_url: str
    collector_url: str
    request_timeout_seconds: float
    deadline_seconds: float


def _positive_float(value: str) -> float:
    result = float(value)
    if not 0 < result <= 120:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 120 seconds")
    return result


def parse_arguments() -> Arguments:
    """Parse bounded CLI/environment inputs."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("SMOKE_GATEWAY_URL", "http://127.0.0.1:8001"),
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
        "--collector-url",
        default=os.getenv("SMOKE_OTEL_COLLECTOR_URL", "http://127.0.0.1:13133"),
    )
    parser.add_argument("--request-timeout", type=_positive_float, default=5.0)
    parser.add_argument("--deadline", type=_positive_float, default=45.0)
    parsed = parser.parse_args()
    return Arguments(
        gateway_url=str(parsed.gateway_url).rstrip("/"),
        prometheus_url=str(parsed.prometheus_url).rstrip("/"),
        loki_url=str(parsed.loki_url).rstrip("/"),
        tempo_url=str(parsed.tempo_url).rstrip("/"),
        collector_url=str(parsed.collector_url).rstrip("/"),
        request_timeout_seconds=float(parsed.request_timeout),
        deadline_seconds=float(parsed.deadline),
    )


async def _poll(
    *,
    description: str,
    deadline_seconds: float,
    check: _PollCheck,
) -> str:
    deadline = time.monotonic() + deadline_seconds
    last_diagnostic = "not checked"
    while time.monotonic() < deadline:
        try:
            complete, diagnostic = await check()
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            complete = False
            diagnostic = f"{type(error).__name__}: {error}"
        if complete:
            return diagnostic
        last_diagnostic = diagnostic
        await asyncio.sleep(0.5)
    raise RuntimeError(f"{description} not proven before deadline; last={last_diagnostic}")


async def _wait_for_http(
    client: httpx.AsyncClient,
    *,
    name: str,
    url: str,
    deadline_seconds: float,
) -> None:
    async def check() -> tuple[bool, str]:
        response = await client.get(url)
        return response.status_code == 200, f"HTTP {response.status_code}: {response.text[:160]}"

    diagnostic = await _poll(
        description=f"{name} readiness", deadline_seconds=deadline_seconds, check=check
    )
    print(f"ready component={name} evidence={diagnostic}")


async def _wait_for_prometheus_targets(
    client: httpx.AsyncClient,
    *,
    prometheus_url: str,
    deadline_seconds: float,
) -> None:
    async def check() -> tuple[bool, str]:
        response = await client.get(f"{prometheus_url}/api/v1/targets")
        response.raise_for_status()
        payload = response.json()
        active_targets = payload["data"]["activeTargets"]
        healthy_jobs = {
            str(target["labels"]["job"])
            for target in active_targets
            if target.get("health") == "up"
        }
        missing = sorted(set(_EXPECTED_ROUTES) - healthy_jobs)
        return not missing, f"healthy_jobs={sorted(healthy_jobs)} missing={missing}"

    diagnostic = await _poll(
        description="Prometheus demo targets",
        deadline_seconds=deadline_seconds,
        check=check,
    )
    print(f"ready component=prometheus-targets evidence={diagnostic}")


def _metric_query(metric: str, service: str, route: str) -> str:
    return f'sum({metric}{{service="{service}",route="{route}"}})'


async def _prometheus_scalar(
    client: httpx.AsyncClient,
    *,
    prometheus_url: str,
    query: str,
) -> float:
    response = await client.get(f"{prometheus_url}/api/v1/query", params={"query": query})
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {payload}")
    result = payload["data"]["result"]
    if not result:
        return 0.0
    return sum(float(sample["value"][1]) for sample in result)


async def _metric_snapshot(
    client: httpx.AsyncClient,
    *,
    prometheus_url: str,
) -> dict[tuple[str, str], float]:
    metrics = ("demo_http_requests_total", "demo_http_request_duration_seconds_count")
    snapshot: dict[tuple[str, str], float] = {}
    for metric in metrics:
        for service, route in _EXPECTED_ROUTES.items():
            snapshot[(metric, service)] = await _prometheus_scalar(
                client,
                prometheus_url=prometheus_url,
                query=_metric_query(metric, service, route),
            )
    return snapshot


async def _prove_metrics(
    client: httpx.AsyncClient,
    *,
    prometheus_url: str,
    baseline: Mapping[tuple[str, str], float],
    deadline_seconds: float,
) -> None:
    async def check() -> tuple[bool, str]:
        current = await _metric_snapshot(client, prometheus_url=prometheus_url)
        missing = {
            f"{metric}:{service}": round(current[(metric, service)] - initial, 3)
            for (metric, service), initial in baseline.items()
            if current[(metric, service)] < initial + 1
        }
        return not missing, f"required_counter_deltas>=1 missing={missing}"

    diagnostic = await _poll(
        description="corresponding request and latency metrics",
        deadline_seconds=deadline_seconds,
        check=check,
    )
    print(f"metrics verified=true evidence={diagnostic}")


def _services_from_loki(payload: Mapping[str, Any]) -> tuple[set[str], int]:
    services: set[str] = set()
    line_count = 0
    for result in payload["data"]["result"]:
        stream = result.get("stream", {})
        service_label = stream.get("service_name")
        if isinstance(service_label, str):
            services.add(service_label)
        for _timestamp, line in result.get("values", []):
            line_count += 1
            try:
                parsed_line = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            service = parsed_line.get("service")
            if isinstance(service, str):
                services.add(service)
    return services, line_count


async def _prove_logs(
    client: httpx.AsyncClient,
    *,
    loki_url: str,
    trace_id: str,
    start_ns: int,
    deadline_seconds: float,
) -> None:
    expected_services = set(_EXPECTED_ROUTES)

    async def check() -> tuple[bool, str]:
        response = await client.get(
            f"{loki_url}/loki/api/v1/query_range",
            params={
                "query": f'{{service_name=~".+"}} |= "{trace_id}"',
                "start": str(start_ns),
                "end": str(time.time_ns()),
                "limit": "100",
                "direction": "backward",
            },
        )
        response.raise_for_status()
        payload = response.json()
        services, line_count = _services_from_loki(payload)
        missing = sorted(expected_services - services)
        return (
            not missing,
            f"trace_id={trace_id} services={sorted(services)} lines={line_count} missing={missing}",
        )

    diagnostic = await _poll(
        description="trace-correlated Loki logs",
        deadline_seconds=deadline_seconds,
        check=check,
    )
    print(f"logs verified=true evidence={diagnostic}")


def _attribute_value(attributes: object, key: str) -> str | None:
    if not isinstance(attributes, list):
        return None
    for attribute in attributes:
        if not isinstance(attribute, dict) or attribute.get("key") != key:
            continue
        value = attribute.get("value")
        if not isinstance(value, dict):
            continue
        for value_key in ("stringValue", "intValue"):
            candidate = value.get(value_key)
            if isinstance(candidate, str):
                return candidate
    return None


def _trace_facts(node: object) -> tuple[set[str], int, set[str]]:
    services: set[str] = set()
    database_systems: set[str] = set()
    span_count = 0

    def visit(value: object) -> None:
        nonlocal span_count
        if isinstance(value, dict):
            resource = value.get("resource")
            if isinstance(resource, dict):
                service = _attribute_value(resource.get("attributes"), "service.name")
                if service is not None:
                    services.add(service)
            if "spanId" in value and "traceId" in value:
                span_count += 1
                attributes = value.get("attributes")
                database_system = _attribute_value(
                    attributes, "db.system.name"
                ) or _attribute_value(attributes, "db.system")
                if database_system is not None:
                    database_systems.add(database_system)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(node)
    return services, span_count, database_systems


async def _prove_trace(
    client: httpx.AsyncClient,
    *,
    tempo_url: str,
    trace_id: str,
    deadline_seconds: float,
) -> None:
    expected_services = set(_EXPECTED_ROUTES)

    async def check() -> tuple[bool, str]:
        response = await client.get(f"{tempo_url}/api/traces/{trace_id}")
        if response.status_code == 404:
            return False, "Tempo returned HTTP 404 while ingestion is pending"
        response.raise_for_status()
        services, span_count, database_systems = _trace_facts(response.json())
        missing = sorted(expected_services - services)
        complete = not missing and span_count >= 7
        return complete, (
            f"trace_id={trace_id} services={sorted(services)} spans={span_count} "
            f"db_systems={sorted(database_systems)} missing={missing}"
        )

    diagnostic = await _poll(
        description="multi-service Tempo trace",
        deadline_seconds=deadline_seconds,
        check=check,
    )
    print(f"trace verified=true evidence={diagnostic}")


async def run_smoke(arguments: Arguments) -> None:
    """Run one checkout and prove metric → log → trace correlation through APIs."""

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(arguments.request_timeout_seconds)
    ) as client:
        readiness_checks = (
            ("gateway", f"{arguments.gateway_url}/health/ready"),
            ("prometheus", f"{arguments.prometheus_url}/-/ready"),
            ("loki", f"{arguments.loki_url}/ready"),
            ("tempo", f"{arguments.tempo_url}/ready"),
            ("otel-collector", arguments.collector_url),
        )
        for name, url in readiness_checks:
            await _wait_for_http(
                client,
                name=name,
                url=url,
                deadline_seconds=arguments.deadline_seconds,
            )
        await _wait_for_prometheus_targets(
            client,
            prometheus_url=arguments.prometheus_url,
            deadline_seconds=arguments.deadline_seconds,
        )
        baseline = await _metric_snapshot(client, prometheus_url=arguments.prometheus_url)

        suffix = uuid4().hex
        request_id = f"observability-smoke-{suffix}"
        started_at_ns = time.time_ns() - 5_000_000_000
        response = await client.post(
            f"{arguments.gateway_url}/checkout",
            json={"customer_id": f"smoke-{suffix}", "sku": "widget-001", "quantity": 2},
            headers={"Idempotency-Key": f"observability-{suffix}", "X-Request-ID": request_id},
        )
        response.raise_for_status()
        checkout = CheckoutResponse.model_validate(response.json())
        trace_id = response.headers.get("X-Trace-ID", "")
        if checkout.request_id != request_id or _TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise RuntimeError(
                f"checkout correlation invalid: request_id={checkout.request_id} trace_id={trace_id!r}"
            )
        print(
            f"checkout verified=true request_id={request_id} trace_id={trace_id} "
            f"payment_id={checkout.payment_id}"
        )

        await _prove_metrics(
            client,
            prometheus_url=arguments.prometheus_url,
            baseline=baseline,
            deadline_seconds=arguments.deadline_seconds,
        )
        await _prove_logs(
            client,
            loki_url=arguments.loki_url,
            trace_id=trace_id,
            start_ns=started_at_ns,
            deadline_seconds=arguments.deadline_seconds,
        )
        await _prove_trace(
            client,
            tempo_url=arguments.tempo_url,
            trace_id=trace_id,
            deadline_seconds=arguments.deadline_seconds,
        )


def main() -> int:
    """CLI entry point with concise diagnostics and a nonzero failure status."""

    try:
        asyncio.run(run_smoke(parse_arguments()))
    except (
        argparse.ArgumentError,
        httpx.HTTPError,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"observability smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
