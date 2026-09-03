"""Bounded slow-database alert, incident, and deterministic evidence scenario."""

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
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError

from packages.incidents import alert_fingerprint
from packages.models.checkout import CheckoutResponse
from packages.models.deployments import DeploymentEnvironment
from packages.models.evidence import (
    CollectionStatus,
    EvidenceItem,
    EvidencePage,
    EvidenceSource,
    EvidenceTimelinePage,
    QueryTemplate,
)
from packages.models.faults import FaultStateResponse
from packages.models.incidents import (
    AlertIngestResponse,
    AuditEventPage,
    IncidentDetail,
    IncidentPage,
    InvestigationRunPage,
    InvestigationRunStatus,
    QueueJobPage,
    QueueJobStatus,
)

ALERT_NAME = "DemoPaymentHighLatency"
PAYMENT_P95_QUERY = (
    "histogram_quantile(0.95, sum by (le) "
    '(rate(demo_http_request_duration_seconds_bucket{service="payment-service",'
    'method="POST",route="/payments"}[20s])))'
)
FAULT_GAUGE_QUERY = 'demo_fault_enabled{service="payment-service",fault="slow_database"}'
TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
EXPECTED_ALERT_FINGERPRINT = alert_fingerprint(
    {
        "alertname": ALERT_NAME,
        "fault": "slow_database",
        "service": "payment-service",
        "severity": "warning",
    }
)


@dataclass(frozen=True)
class Arguments:
    """Validated local endpoints and fixed scenario bounds."""

    gateway_url: str
    payment_url: str
    prometheus_url: str
    loki_url: str
    tempo_url: str
    alertmanager_url: str
    incident_api_url: str
    environment: DeploymentEnvironment
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


@dataclass(frozen=True)
class IncidentBaseline:
    """Existing durable state captured before this bounded scenario run."""

    occurrence_count: int
    run_ids: frozenset[str]
    job_ids: frozenset[str]
    evidence_ids: frozenset[str]


@dataclass(frozen=True)
class IncidentEvidence:
    """Durable incident, completed evidence run, and queue-job proof."""

    incident: IncidentDetail
    run_id: str
    job_id: str
    source_summary: str


async def _all_incident_evidence(
    client: httpx.AsyncClient,
    arguments: Arguments,
    incident_id: str,
) -> tuple[EvidenceItem, ...]:
    """Read every bounded public page so repeated scenario runs cannot hide new evidence."""

    items: list[EvidenceItem] = []
    offset = 0
    while True:
        response = await client.get(
            f"{arguments.incident_api_url}/api/v1/incidents/{incident_id}/evidence",
            params={"limit": "100", "offset": str(offset)},
        )
        _require_success(response, "Incident API evidence")
        page = EvidencePage.model_validate(response.json())
        if page.total > 5_000:
            raise RuntimeError("incident evidence exceeds the scenario safety bound")
        items.extend(page.items)
        if len(items) >= page.total:
            return tuple(items)
        if not page.items:
            raise RuntimeError("Incident API evidence pagination made no progress")
        offset += len(page.items)


async def _register_demo_deployments(
    client: httpx.AsyncClient,
    arguments: Arguments,
) -> None:
    """Register neutral local version history through the public deterministic API."""

    anchor = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    deployments = (
        {
            "service": "payment-service",
            "environment": arguments.environment.value,
            "version": "0.0.9",
            "deployed_at": (anchor - timedelta(minutes=90)).isoformat(),
            "commit_sha": "1" * 40,
            "changed_files": ["apps/demo/payment_service/main.py"],
            "metadata": {"scenario": "slow_database", "role": "previous_baseline"},
        },
        {
            "service": "payment-service",
            "environment": arguments.environment.value,
            "version": "0.1.0",
            "deployed_at": (anchor - timedelta(minutes=10)).isoformat(),
            "commit_sha": "2" * 40,
            "changed_files": ["apps/demo/payment_service/faults.py"],
            "metadata": {"scenario": "slow_database", "role": "current_baseline"},
        },
    )
    for deployment in deployments:
        response = await client.post(
            f"{arguments.incident_api_url}/api/v1/deployments",
            json=deployment,
        )
        _require_success(response, "demo deployment registration")
    print("deployments registered=true service=payment-service versions=0.0.9,0.1.0")


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
        "--incident-api-url",
        default=os.getenv("SCENARIO_INCIDENT_API_URL", "http://127.0.0.1:8006"),
    )
    parser.add_argument(
        "--fault-control-token",
        default=os.getenv(
            "SCENARIO_FAULT_CONTROL_TOKEN",
            os.getenv("FAULT_CONTROL_TOKEN", "local-demo-fault-control"),
        ),
    )
    parser.add_argument(
        "--environment",
        choices=[environment.value for environment in DeploymentEnvironment],
        default=os.getenv("ENVIRONMENT", DeploymentEnvironment.DEVELOPMENT.value),
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
        incident_api_url=str(parsed.incident_api_url).rstrip("/"),
        environment=DeploymentEnvironment(str(parsed.environment)),
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
    request_id = f"stage05-{label}-{suffix}"
    started_at_ns = time.time_ns() - 1_000_000_000
    started_at = time.perf_counter()
    response = await client.post(
        f"{arguments.gateway_url}/checkout",
        json={"customer_id": "stage03-customer", "sku": "widget-001", "quantity": 1},
        headers={
            "Idempotency-Key": f"stage05-{label}-{suffix}",
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
) -> dict[str, object]:
    async def probe() -> dict[str, object] | None:
        response = await client.get(f"{arguments.alertmanager_url}/api/v2/alerts")
        _require_success(response, "Alertmanager alerts query")
        alerts = response.json()
        for alert in alerts:
            if (
                isinstance(alert, dict)
                and alert.get("labels", {}).get("alertname") == ALERT_NAME
                and alert.get("status", {}).get("state") == "active"
            ):
                return alert
        raise ValueError(f"Alertmanager has no active {ALERT_NAME}")

    return await _wait_for(
        "Alertmanager active alert",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _matching_incident(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    fingerprint: str,
) -> IncidentDetail | None:
    response = await client.get(
        f"{arguments.incident_api_url}/api/v1/incidents",
        params={"limit": "100", "offset": "0"},
    )
    _require_success(response, "Incident API list")
    page = IncidentPage.model_validate(response.json())
    matches = [item for item in page.items if item.alert_fingerprint == fingerprint]
    if len(matches) > 1:
        raise RuntimeError("stable alert fingerprint mapped to more than one durable incident")
    if not matches:
        return None
    detail_response = await client.get(
        f"{arguments.incident_api_url}/api/v1/incidents/{matches[0].id}"
    )
    _require_success(detail_response, "Incident API detail")
    return IncidentDetail.model_validate(detail_response.json())


async def _incident_baseline(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    fingerprint: str,
) -> IncidentBaseline:
    incident = await _matching_incident(client, arguments, fingerprint=fingerprint)
    if incident is None:
        return IncidentBaseline(
            occurrence_count=0,
            run_ids=frozenset(),
            job_ids=frozenset(),
            evidence_ids=frozenset(),
        )
    runs_response = await client.get(
        f"{arguments.incident_api_url}/api/v1/incidents/{incident.id}/investigation-runs",
        params={"limit": "100"},
    )
    jobs_response = await client.get(
        f"{arguments.incident_api_url}/api/v1/investigation-jobs",
        params={"incident_id": incident.id, "limit": "100"},
    )
    _require_success(runs_response, "Incident API baseline runs")
    _require_success(jobs_response, "Incident API baseline jobs")
    runs = InvestigationRunPage.model_validate(runs_response.json())
    jobs = QueueJobPage.model_validate(jobs_response.json())
    evidence = await _all_incident_evidence(client, arguments, incident.id)
    return IncidentBaseline(
        occurrence_count=incident.occurrence_count,
        run_ids=frozenset(str(run.id) for run in runs.items),
        job_ids=frozenset(str(job.id) for job in jobs.items),
        evidence_ids=frozenset(item.id for item in evidence),
    )


async def _wait_for_incident_processed(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    fingerprint: str,
    baseline: IncidentBaseline,
) -> IncidentEvidence:
    async def probe() -> IncidentEvidence | None:
        incident = await _matching_incident(client, arguments, fingerprint=fingerprint)
        if incident is None:
            raise ValueError("Incident API has not persisted the alert")
        if incident.occurrence_count <= baseline.occurrence_count:
            raise ValueError(
                f"incident occurrence_count={incident.occurrence_count} has not advanced"
            )
        runs_response = await client.get(
            f"{arguments.incident_api_url}/api/v1/incidents/{incident.id}/investigation-runs",
            params={"limit": "100"},
        )
        jobs_response = await client.get(
            f"{arguments.incident_api_url}/api/v1/investigation-jobs",
            params={"incident_id": incident.id, "limit": "100"},
        )
        timeline_response = await client.get(
            f"{arguments.incident_api_url}/api/v1/incidents/{incident.id}/timeline",
            params={"limit": "100"},
        )
        evidence_timeline_response = await client.get(
            f"{arguments.incident_api_url}/api/v1/incidents/{incident.id}/evidence/timeline",
            params={"limit": "100"},
        )
        _require_success(runs_response, "Incident API investigation runs")
        _require_success(jobs_response, "Incident API queue jobs")
        _require_success(timeline_response, "Incident API timeline")
        _require_success(evidence_timeline_response, "Incident API evidence timeline")
        runs = InvestigationRunPage.model_validate(runs_response.json())
        jobs = QueueJobPage.model_validate(jobs_response.json())
        timeline = AuditEventPage.model_validate(timeline_response.json())
        evidence_items = await _all_incident_evidence(client, arguments, incident.id)
        evidence_timeline = EvidenceTimelinePage.model_validate(evidence_timeline_response.json())
        completed_runs = [
            run
            for run in runs.items
            if str(run.id) not in baseline.run_ids
            and run.status == InvestigationRunStatus.EVIDENCE_COLLECTED
        ]
        completed_jobs = [
            job
            for job in jobs.items
            if str(job.id) not in baseline.job_ids and job.status == QueueJobStatus.COMPLETED
        ]
        if not completed_runs or not completed_jobs:
            raise ValueError(
                "evidence collection not complete yet: "
                f"run_statuses={[run.status for run in runs.items]} "
                f"job_statuses={[job.status for job in jobs.items]}"
            )
        if not any(
            event.event_type == "investigation.evidence_collection_completed"
            and event.details.get("ai_executed") is False
            for event in timeline.items
        ):
            raise ValueError("timeline lacks the explicit deterministic evidence completion event")
        new_evidence = [item for item in evidence_items if item.id not in baseline.evidence_ids]
        expected_sources = set(EvidenceSource)
        actual_sources = {item.source for item in new_evidence}
        if actual_sources != expected_sources:
            raise ValueError(
                "evidence sources are incomplete: "
                f"expected={sorted(source.value for source in expected_sources)} "
                f"actual={sorted(source.value for source in actual_sources)}"
            )
        sources_without_collected_evidence = {
            source
            for source in expected_sources
            if not any(
                item.source == source and item.status == CollectionStatus.COLLECTED
                for item in new_evidence
            )
        }
        if sources_without_collected_evidence:
            raise ValueError(
                "controlled scenario produced no collected evidence for: "
                + ",".join(sorted(source.value for source in sources_without_collected_evidence))
            )
        required_collected_templates = {
            QueryTemplate.METRIC_SERVICE_LATENCY,
            QueryTemplate.LOG_GROUPED_PATTERNS,
            QueryTemplate.LOG_AROUND_TIMESTAMP,
            QueryTemplate.TRACE_SLOW_SERVICE,
            QueryTemplate.TRACE_SERVICE_DEPENDENCIES,
            QueryTemplate.DEPLOYMENT_RECENT,
            QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
        }
        collected_templates = {
            item.query_template
            for item in new_evidence
            if item.status == CollectionStatus.COLLECTED
        }
        if missing_templates := required_collected_templates - collected_templates:
            raise ValueError(
                "controlled scenario lacks collected domain evidence for: "
                + ",".join(sorted(template.value for template in missing_templates))
            )
        if not evidence_timeline.items:
            raise ValueError("evidence timeline contains no correlated events")
        timeline_keys = [
            (event.timestamp, event.source.value, event.evidence_id, event.id)
            for event in evidence_timeline.items
        ]
        if timeline_keys != sorted(timeline_keys):
            raise RuntimeError("evidence timeline ordering is not deterministic")
        if incident.root_cause is not None or incident.confidence is not None:
            raise RuntimeError("deterministic evidence collection fabricated an AI result")
        status_counts: dict[str, dict[str, int]] = {}
        for item in new_evidence:
            source_counts = status_counts.setdefault(item.source.value, {})
            source_counts[item.status.value] = source_counts.get(item.status.value, 0) + 1
        source_summary = ";".join(
            f"{source}=" + ",".join(f"{status}:{count}" for status, count in sorted(counts.items()))
            for source, counts in sorted(status_counts.items())
        )
        return IncidentEvidence(
            incident=incident,
            run_id=str(completed_runs[0].id),
            job_id=str(completed_jobs[0].id),
            source_summary=source_summary,
        )

    return await _wait_for(
        "durable incident and deterministic evidence collection",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def _prove_duplicate_delivery(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    alertmanager_alert: dict[str, object],
    evidence: IncidentEvidence,
) -> None:
    labels = alertmanager_alert.get("labels")
    annotations = alertmanager_alert.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise RuntimeError("Alertmanager API returned malformed alert labels/annotations")
    alert_payload = {
        "status": "firing",
        "labels": labels,
        "annotations": annotations,
        "startsAt": alertmanager_alert.get("startsAt"),
        "endsAt": alertmanager_alert.get("endsAt", "0001-01-01T00:00:00Z"),
        "generatorURL": alertmanager_alert.get("generatorURL", ""),
        "fingerprint": alertmanager_alert.get("fingerprint", ""),
    }
    webhook = {
        "version": "4",
        "status": "firing",
        "receiver": "incident-api",
        "alerts": [alert_payload],
    }
    for attempt in range(2):
        response = await client.post(
            f"{arguments.incident_api_url}/api/v1/alerts",
            json=webhook,
            headers={"X-Request-ID": f"stage05-duplicate-{attempt + 1}"},
        )
        _require_success(response, "duplicate Alertmanager delivery")
        body = AlertIngestResponse.model_validate(response.json())
        if not body.alerts[0].duplicate or body.alerts[0].investigation_enqueued:
            raise RuntimeError("duplicate delivery was not an idempotent no-op")
    after = await _matching_incident(
        client,
        arguments,
        fingerprint=evidence.incident.alert_fingerprint,
    )
    if after is None or after.occurrence_count != evidence.incident.occurrence_count:
        raise RuntimeError("duplicate delivery changed durable occurrence count")
    runs_response = await client.get(
        f"{arguments.incident_api_url}/api/v1/incidents/{evidence.incident.id}/investigation-runs",
        params={"limit": "100"},
    )
    jobs_response = await client.get(
        f"{arguments.incident_api_url}/api/v1/investigation-jobs",
        params={"incident_id": evidence.incident.id, "limit": "100"},
    )
    _require_success(runs_response, "post-duplicate investigation runs")
    _require_success(jobs_response, "post-duplicate queue jobs")
    runs = InvestigationRunPage.model_validate(runs_response.json())
    jobs = QueueJobPage.model_validate(jobs_response.json())
    if sum(str(run.id) == evidence.run_id for run in runs.items) != 1:
        raise RuntimeError("duplicate delivery changed the evidence run")
    if sum(str(job.id) == evidence.job_id for job in jobs.items) != 1:
        raise RuntimeError("duplicate delivery changed the queue job")


async def _wait_for_incident_resolved(
    client: httpx.AsyncClient,
    arguments: Arguments,
    *,
    evidence: IncidentEvidence,
) -> IncidentDetail:
    async def probe() -> IncidentDetail | None:
        incident = await _matching_incident(
            client,
            arguments,
            fingerprint=evidence.incident.alert_fingerprint,
        )
        if incident is None or incident.status.value != "resolved":
            observed = None if incident is None else incident.status.value
            raise ValueError(f"durable incident status is {observed}, not resolved")
        if incident.occurrence_count <= evidence.incident.occurrence_count:
            raise ValueError("resolved Alertmanager update was not stored as an occurrence")
        return incident

    return await _wait_for(
        "durable resolved incident update",
        deadline_seconds=arguments.poll_deadline_seconds,
        probe=probe,
    )


async def run_scenario(arguments: Arguments) -> None:
    """Prove fault, alert, durable evidence collection, deduplication, and recovery."""

    timeout = httpx.Timeout(arguments.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        readiness_checks = (
            ("gateway readiness", f"{arguments.gateway_url}/health/ready"),
            ("payment readiness", f"{arguments.payment_url}/health/ready"),
            ("Prometheus readiness", f"{arguments.prometheus_url}/-/ready"),
            ("Loki readiness", f"{arguments.loki_url}/ready"),
            ("Tempo readiness", f"{arguments.tempo_url}/ready"),
            ("Alertmanager readiness", f"{arguments.alertmanager_url}/-/ready"),
            ("Incident API readiness", f"{arguments.incident_api_url}/health/ready"),
        )
        for name, url in readiness_checks:
            await _wait_for_http_ready(
                client,
                name=name,
                url=url,
                deadline_seconds=arguments.poll_deadline_seconds,
            )

        await _register_demo_deployments(client, arguments)

        await _set_fault(client, arguments, enabled=False)
        await _wait_for_alert_recovery(client, arguments)
        incident_baseline = await _incident_baseline(
            client,
            arguments,
            fingerprint=EXPECTED_ALERT_FINGERPRINT,
        )

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
            alertmanager_alert = await _wait_for_alertmanager_alert(client, arguments)
            labels = alertmanager_alert.get("labels")
            if not isinstance(labels, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
            ):
                raise RuntimeError("Alertmanager returned invalid labels")
            actual_fingerprint = alert_fingerprint(labels)
            if actual_fingerprint != EXPECTED_ALERT_FINGERPRINT:
                raise RuntimeError(
                    "alert labels differ from the repository rule; "
                    f"expected_fingerprint={EXPECTED_ALERT_FINGERPRINT} "
                    f"actual_fingerprint={actual_fingerprint}"
                )
            incident_evidence = await _wait_for_incident_processed(
                client,
                arguments,
                fingerprint=actual_fingerprint,
                baseline=incident_baseline,
            )
            await _prove_duplicate_delivery(
                client,
                arguments,
                alertmanager_alert=alertmanager_alert,
                evidence=incident_evidence,
            )
            print(
                f"alert name={ALERT_NAME} prometheus=firing alertmanager=active "
                f"p95_seconds={p95:.3f} incident_id={incident_evidence.incident.id} "
                f"occurrences={incident_evidence.incident.occurrence_count} "
                f"run_id={incident_evidence.run_id} job_id={incident_evidence.job_id} "
                f"evidence=evidence_collected sources={incident_evidence.source_summary} "
                "ai_executed=false duplicate_delivery=idempotent"
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
        resolved_incident = await _wait_for_incident_resolved(
            client,
            arguments,
            evidence=incident_evidence,
        )
        print(
            f"recovery fault_gauge={gauge_value:.0f} "
            f"latency_seconds={recovered.latency_seconds:.3f} "
            f"prometheus=inactive incident_id={resolved_incident.id} "
            f"incident_status={resolved_incident.status.value} "
            f"occurrences={resolved_incident.occurrence_count}"
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
