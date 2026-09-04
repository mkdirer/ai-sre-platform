"""Eval runners: deterministic fake suite for CI + gated live runner (Stage 09)."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from packages.evals.artifacts import write_eval_artifacts
from packages.evals.fixtures import run_fake_scenario
from packages.evals.grading import (
    EvalSummary,
    RunMetadata,
    ScenarioGrade,
    grade_report,
    summarize,
)
from packages.evals.scenario import EvalScenario, load_enabled_scenarios
from packages.models.investigation import IncidentReport


@dataclass(frozen=True)
class FakeDatasetResult:
    """Offline deterministic outcome for one dataset version."""

    grades: list[ScenarioGrade]
    metadatas: list[RunMetadata]
    reports: list[IncidentReport]
    scenarios: list[EvalScenario]
    summary: EvalSummary


async def run_fake_dataset(
    scenarios_dir: Path, *, dataset_version: str | None = None
) -> FakeDatasetResult:
    """Run every enabled scenario offline with fixture evidence + fake provider."""

    scenarios = load_enabled_scenarios(scenarios_dir)
    if dataset_version is not None and dataset_version != "all":
        if dataset_version == "v1-extended":
            # Extended builds on v1: include core plus edge fixtures.
            scenarios = [
                item for item in scenarios if item.dataset_version in ("v1", "v1-extended")
            ]
        else:
            scenarios = [item for item in scenarios if item.dataset_version == dataset_version]
    grades: list[ScenarioGrade] = []
    metadatas: list[RunMetadata] = []
    reports: list[IncidentReport] = []
    for scenario in scenarios:
        report, metadata, evidence = await run_fake_scenario(scenario)
        known_ids = {item.id for item in evidence}
        templates = {
            item.query_template.value for item in evidence if str(item.status) == "collected"
        }
        sources = {item.source.value for item in evidence if str(item.status) == "collected"}
        grade = grade_report(
            scenario,
            report,
            known_evidence_ids=known_ids,
            collected_templates=templates,
            collected_sources=sources,
            metadata=metadata,
        )
        grades.append(grade)
        metadatas.append(metadata)
        reports.append(report)
    resolved_version = dataset_version or (scenarios[0].dataset_version if scenarios else "v1")
    summary = summarize(resolved_version, grades, metadatas)
    return FakeDatasetResult(
        grades=grades, metadatas=metadatas, reports=reports, scenarios=scenarios, summary=summary
    )


def run_fake_dataset_sync(
    scenarios_dir: Path, *, dataset_version: str | None = None
) -> FakeDatasetResult:
    """Synchronous wrapper for scripts and tests."""

    return asyncio.run(run_fake_dataset(scenarios_dir, dataset_version=dataset_version))


def write_fake_dataset_artifacts(
    result: FakeDatasetResult, *, output_dir: Path, model_config: str = "fake"
) -> tuple[Path, Path]:
    """Persist JSON + Markdown tied to dataset, commit, and model config."""

    return write_eval_artifacts(
        output_dir=output_dir,
        dataset_version=result.summary.dataset_version,
        model_config=model_config,
        grades=result.grades,
        metadatas=result.metadatas,
        summary=result.summary,
        scenario_ids=[scenario.scenario_id for scenario in result.scenarios],
    )


@dataclass(frozen=True)
class LiveEvalConfig:
    """Bounded live-runner endpoints and safety bounds."""

    gateway_url: str
    payment_url: str
    inventory_url: str
    incident_api_url: str
    prometheus_url: str
    fault_control_token: str
    request_timeout_seconds: float = 10.0
    poll_deadline_seconds: float = 60.0
    max_cost_usd: float = 0.0


@asynccontextmanager
async def activated_fault(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    control_path: str,
    token: str,
) -> AsyncIterator[None]:
    """Enable one fault and guarantee a disable attempt on every exit path."""

    enable = await client.put(
        f"{base_url}{control_path}",
        json={"enabled": True},
        headers={"X-Fault-Control-Token": token},
    )
    if enable.is_error:
        raise RuntimeError(
            f"enable fault {control_path} failed: {enable.status_code} {enable.text}"
        )
    try:
        yield
    except BaseException as body_error:
        disable = await client.put(
            f"{base_url}{control_path}",
            json={"enabled": False},
            headers={"X-Fault-Control-Token": token},
        )
        if disable.is_error:
            raise RuntimeError(
                f"disable fault {control_path} failed: {disable.status_code} {disable.text}"
            ) from body_error
        raise
    disable = await client.put(
        f"{base_url}{control_path}",
        json={"enabled": False},
        headers={"X-Fault-Control-Token": token},
    )
    if disable.is_error:
        raise RuntimeError(
            f"disable fault {control_path} failed: {disable.status_code} {disable.text}"
        )


_KNOWN_FAULT_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "payment",
        (
            "slow-database",
            "pool-exhaustion",
            "bad-deployment",
            "cpu-saturation",
            "high-error-rate",
        ),
    ),
    ("inventory", ("inventory-timeout",)),
)


async def _reset_faults(client: httpx.AsyncClient, config: LiveEvalConfig) -> bool:
    """Best-effort disable of every known demo fault (environment reset).

    Never raises: a stale fault must not mask the scenario's own activation
    error, so failures are reported by the caller as notes.
    """

    base_by_service = {"payment": config.payment_url, "inventory": config.inventory_url}
    ok = True
    for service, names in _KNOWN_FAULT_PATHS:
        for name in names:
            try:
                response = await client.put(
                    f"{base_by_service[service]}/internal/faults/{name}",
                    json={"enabled": False},
                    headers={"X-Fault-Control-Token": config.fault_control_token},
                )
            except Exception:
                ok = False
            else:
                ok = ok and not response.is_error
    return ok


async def _wait_for_incident(
    client: httpx.AsyncClient,
    incident_api_url: str,
    *,
    since: datetime,
    deadline_seconds: float,
) -> dict[str, object] | None:
    """Poll the incident list for an incident created after `since`."""

    deadline = time.perf_counter() + max(0.0, deadline_seconds)
    while True:
        response = await client.get(f"{incident_api_url}/api/v1/incidents", params={"limit": 5})
        if not response.is_error:
            payload = response.json()
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                raw = str(item.get("created_at", ""))
                if raw.endswith(("Z", "z")):
                    raw = raw[:-1] + "+00:00"
                try:
                    created = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created >= since:
                    return item
        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(2.0)


async def _wait_for_report(
    client: httpx.AsyncClient,
    incident_api_url: str,
    incident_id: str,
    *,
    deadline_seconds: float,
) -> dict[str, object] | None:
    """Poll the structured report until present (404 while pending) or deadline."""

    deadline = time.perf_counter() + max(0.0, deadline_seconds)
    while True:
        response = await client.get(f"{incident_api_url}/api/v1/incidents/{incident_id}/report")
        if response.status_code == 200:
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(2.0)


async def _fetch_evidence_sets(
    client: httpx.AsyncClient, incident_api_url: str, incident_id: str
) -> tuple[set[str], set[str], set[str]]:
    """Fetch one evidence page and derive known IDs, templates, and sources."""

    response = await client.get(
        f"{incident_api_url}/api/v1/incidents/{incident_id}/evidence",
        params={"limit": 100},
    )
    if response.is_error:
        return set(), set(), set()
    payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    known: set[str] = set()
    templates: set[str] = set()
    sources: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("id"):
            known.add(str(item["id"]))
        if item.get("query_template"):
            templates.add(str(item["query_template"]))
        if item.get("source"):
            sources.add(str(item["source"]))
    return known, templates, sources


async def _bounded_checkout(
    client: httpx.AsyncClient, gateway_url: str, *, label: str
) -> dict[str, object]:
    """Generate one bounded checkout and record the outcome without aborting.

    HTTP error statuses (e.g. deterministic 500s from the high_error_rate
    fault) are valid eval signals and are recorded; only transport-level
    exceptions propagate. This keeps live SCN-006 traffic alive: roughly half
    of its checkouts are expected to fail by design.
    """

    suffix = uuid4().hex
    response = await client.post(
        f"{gateway_url}/checkout",
        json={"customer_id": "eval-customer", "sku": "widget-001", "quantity": 1},
        headers={
            "Idempotency-Key": f"eval-{label}-{suffix}",
            "X-Request-ID": f"eval-{label}-{suffix}",
        },
    )
    outcome: dict[str, object] = {"label": label, "status": response.status_code}
    if response.is_error:
        outcome["error"] = response.text[:200]
    return outcome


async def run_live_scenario(scenario: EvalScenario, config: LiveEvalConfig) -> dict[str, object]:
    """Run one bounded live scenario with guaranteed fault cleanup.

    Steps: validate readiness and best-effort reset faults, register
    deployments, activate one fault, generate bounded traffic, wait with
    deadlines for an incident and its structured report when the scenario
    expects an alert, grade the report when retrieved, disable the fault,
    and return a diagnostic artifact dict. Never runs unbounded load.

    Only slow_database variants fire DemoPaymentHighLatency today, so
    scenarios without an expected alert skip the incident/report wait and
    record why; see ADR-0010.
    """

    timeout = httpx.Timeout(config.request_timeout_seconds)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    notes: list[str] = []
    artifact: dict[str, object] = {
        "scenario_id": scenario.scenario_id,
        "fault": scenario.fault.name,
        "traffic": [],
        "incident_id": None,
        "report_present": False,
        "grade_passed": None,
        "cleaned_up": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. Readiness (bounded) + best-effort reset of stale faults.
        for name, url in (
            ("gateway", f"{config.gateway_url}/health/ready"),
            ("incident-api", f"{config.incident_api_url}/health/ready"),
        ):
            response = await client.get(url)
            if response.is_error:
                raise RuntimeError(f"live eval readiness {name} failed: {response.status_code}")
        if not await _reset_faults(client, config):
            notes.append("pre-run fault reset incomplete; continuing with activation")
        # 2. Register expected deployments through the public API.
        for deployment in scenario.deployments:
            deployed_at = datetime.now(UTC) + timedelta(
                minutes=deployment.deployed_at_offset_minutes
            )
            body = {
                "service": deployment.service,
                "environment": deployment.environment,
                "version": deployment.version,
                "deployed_at": deployed_at.isoformat(),
                "commit_sha": deployment.commit_sha,
                "changed_files": ["evals/live"],
                "metadata": {"scenario": scenario.scenario_id, "role": deployment.role},
            }
            response = await client.post(f"{config.incident_api_url}/api/v1/deployments", json=body)
            # Exact replay is idempotent; conflict with different content is diagnostic.
            if response.is_error and response.status_code not in (200, 201, 409):
                raise RuntimeError(f"deployment registration failed: {response.text[:200]}")
        # 3-7. Activate fault + bounded traffic + waits inside the cleanup context.
        base_url = (
            config.inventory_url
            if scenario.fault.service == "inventory-service"
            else config.payment_url
        )
        control_path = scenario.fault.control_path or f"/internal/faults/{scenario.fault.name}"
        if scenario.fault.name == "healthy":
            # No fault to activate; still generate a smaller baseline sample.
            healthy_traffic: list[dict[str, object]] = []
            for index in range(min(scenario.traffic.count, 4)):
                healthy_traffic.append(
                    await _bounded_checkout(client, config.gateway_url, label=f"healthy-{index}")
                )
            artifact["traffic"] = healthy_traffic
            notes.append("healthy scenario: no fault activated, no incident expected")
        else:
            async with activated_fault(
                client,
                base_url=base_url,
                control_path=control_path,
                token=config.fault_control_token,
            ):
                traffic: list[dict[str, object]] = []
                for index in range(scenario.traffic.count):
                    traffic.append(
                        await _bounded_checkout(
                            client, config.gateway_url, label=f"{scenario.scenario_id}-{index}"
                        )
                    )
                artifact["traffic"] = traffic
                if not scenario.fault.expect_alert:
                    notes.append(
                        "no alert expected for this fault (no per-fault Prometheus "
                        "alert; see ADR-0010); incident/report wait skipped"
                    )
                else:
                    incident = await _wait_for_incident(
                        client,
                        config.incident_api_url,
                        since=started_at,
                        deadline_seconds=scenario.traffic.poll_deadline_seconds,
                    )
                    elapsed = time.perf_counter() - started
                    remaining = max(0.0, scenario.traffic.poll_deadline_seconds - elapsed)
                    if incident is None:
                        raise RuntimeError(
                            f"live scenario {scenario.scenario_id}: no incident within "
                            f"{scenario.traffic.poll_deadline_seconds:.0f}s deadline"
                        )
                    incident_id = str(incident["id"])
                    artifact["incident_id"] = incident_id
                    report_payload = await _wait_for_report(
                        client,
                        config.incident_api_url,
                        incident_id,
                        deadline_seconds=remaining,
                    )
                    if report_payload is None:
                        notes.append(f"no structured report within deadline for {incident_id}")
                    else:
                        artifact["report_present"] = True
                        try:
                            report = IncidentReport.model_validate(report_payload)
                        except Exception as error:
                            notes.append(f"live report failed validation: {error}")
                        else:
                            known, templates, sources = await _fetch_evidence_sets(
                                client, config.incident_api_url, incident_id
                            )
                            grade = grade_report(
                                scenario,
                                report,
                                known_evidence_ids=known,
                                collected_templates=templates,
                                collected_sources=sources,
                                metadata=RunMetadata(
                                    duration_seconds=time.perf_counter() - started
                                ),
                            )
                            artifact["grade_passed"] = grade.passed
                            artifact["grade_notes"] = list(grade.notes)
        artifact["cleaned_up"] = True
    artifact["duration_seconds"] = time.perf_counter() - started
    artifact["notes"] = notes
    return artifact
