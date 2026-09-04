"""Live remediation scenario: approved rollback to verified recovery (Stage 10).

Complete local story against Compose: register bad/previous deployments,
enable the bad_deployment fault, bounded traffic, incident via a synthetic
firing webhook (only slow_database owns a Prometheus rule; see ADR-0010),
production evidence collection and investigation over live telemetry, real
approval/execution APIs, real worker remediation task, deterministic
telemetry verification, resolved incident. Always attempts fault cleanup.

Stand-ins, documented honestly: the firing webhook (no per-fault alert
rule) and the scripted provider (no paid model calls). Every other line
exercised — claim/collect/complete, approval, execution, adapter,
verification — is production code against live services.

Usage:
    docker compose up --build -d
    uv run python scripts/scenario_remediation.py
    uv run python scripts/scenario_remediation.py --run-label second
"""

import argparse
import asyncio
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from packages.agents.provider import BudgetedModelGateway, ProviderResult, StructuredModelProvider
from packages.agents.validation import stable_hypothesis_id
from packages.agents.workflow import InvestigatorWorkflow
from packages.config import Settings
from packages.incidents.evidence_collection import EvidenceAdapters, EvidenceCollectionService
from packages.models.evidence import EvidenceService
from packages.models.investigation import (
    HypothesisCandidate,
    HypothesisCandidates,
    HypothesisStatus,
    HypothesisVerification,
    RecommendationAction,
    RecommendationProposal,
    RecommendationRisk,
    ReportSynthesis,
    RootCauseCategory,
    RunUsage,
)
from packages.persistence import (
    SqlAlchemyEvidenceStore,
    SqlAlchemyIncidentStore,
    SqlAlchemyInvestigationStore,
    WorkerClaim,
)
from packages.telemetry import TelemetryRuntime
from packages.tools.deployments import DeploymentAdapter, DeploymentClient
from packages.tools.loki import LokiAdapter, LokiClient
from packages.tools.prometheus import PrometheusAdapter, PrometheusClient
from packages.tools.tempo import TempoAdapter, TempoClient

PAYMENT = "http://127.0.0.1:8004"
GATEWAY = "http://127.0.0.1:8001"
INCIDENT_API = "http://127.0.0.1:8006"
BAD_VERSION = "0.2.0"
GOOD_VERSION = "0.1.0"


class _ScriptedProvider(StructuredModelProvider):
    """Deterministic stand-in for model weights; everything else is production."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self._script = script

    @property
    def name(self) -> str:
        return "scripted-scenario"

    async def complete(  # type: ignore[override]
        self,
        *,
        model: str,
        instructions: str,
        input_json: str,
        response_model: type[BaseModel],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult[BaseModel]:
        queue = self._script[response_model.__name__]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        assert isinstance(item, BaseModel)
        return ProviderResult(
            output=item, response_id="scenario-response", input_tokens=10, output_tokens=10
        )

    async def close(self) -> None:
        return None


def _check(step: str, response: httpx.Response, *, expect: int = 200) -> dict[str, object]:
    if response.status_code != expect:
        raise RuntimeError(f"{step} failed: HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def _wait_for(
    label: str, timeout_seconds: float, poll: Callable[[], Awaitable[object]], interval: float = 2.0
) -> object:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        found = await poll()
        if found:
            return found
        await asyncio.sleep(interval)
    raise RuntimeError(f"timed out waiting for {label} ({timeout_seconds:.0f}s)")


async def _already_collected(claim: WorkerClaim) -> tuple[object, ...]:
    """Collector seam for evidence the worker path already persisted."""

    del claim
    return ()


def _compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-label", default="first")
    parser.add_argument("--traffic", type=int, default=8)
    args = parser.parse_args()
    label = args.run_label
    timings: dict[str, float] = {}
    started_all = time.perf_counter()
    settings = Settings()
    token = settings.fault_control_token.get_secret_value()
    timeout = httpx.Timeout(10.0)
    incident_store = SqlAlchemyIncidentStore(settings)
    evidence_store = SqlAlchemyEvidenceStore(settings)
    artifact_store = SqlAlchemyInvestigationStore(settings)
    worker_stopped = False

    def _mark(step: str) -> None:
        timings[step] = time.perf_counter() - started_all
        print(f"[{timings[step]:6.1f}s] {step}", flush=True)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for name, url in (
                ("gateway", f"{GATEWAY}/health/ready"),
                ("incident-api", f"{INCIDENT_API}/health/ready"),
            ):
                response = await client.get(url)
                if response.is_error:
                    raise RuntimeError(f"readiness {name} failed: {response.status_code}")
            _mark("readiness ok")

            _compose("stop", "investigator-worker")
            deadline = time.perf_counter() + 60.0
            while time.perf_counter() < deadline:
                exited = subprocess.run(
                    ["docker", "compose", "ps", "--format", "{{.Status}}", "investigator-worker"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                status = exited.stdout.strip().lower()
                if not status or "exited" in status or "stopped" in status:
                    break
                await asyncio.sleep(2.0)
            else:
                raise RuntimeError("worker did not stop within 60s")
            worker_stopped = True
            _mark("worker stopped for deterministic claim")

            now = datetime.now(UTC)
            for version, offset in ((GOOD_VERSION, 60), (BAD_VERSION, 2)):
                body = {
                    "service": "payment-service",
                    "environment": "development",
                    "version": version,
                    "deployed_at": (now - timedelta(minutes=offset)).isoformat(),
                    "commit_sha": ("b" if version == GOOD_VERSION else "a") * 40,
                    "changed_files": ["apps/demo/payment_service/main.py"],
                    "metadata": {"scenario": f"remediation-{label}"},
                }
                response = await client.post(f"{INCIDENT_API}/api/v1/deployments", json=body)
                if response.is_error and response.status_code not in (200, 201, 409):
                    raise RuntimeError(f"deployment register failed: {response.text[:200]}")
            _mark("deployments registered (0.1.0 previous, 0.2.0 current)")

            response = await client.put(
                f"{PAYMENT}/internal/faults/bad-deployment",
                json={"enabled": True},
                headers={"X-Fault-Control-Token": token},
            )
            if response.is_error:
                raise RuntimeError(f"fault enable failed: {response.text[:200]}")
            _mark("bad_deployment fault enabled")

            for index in range(args.traffic):
                await client.post(
                    f"{GATEWAY}/checkout",
                    json={"customer_id": "remediation-demo", "sku": "widget-001", "quantity": 1},
                    headers={"Idempotency-Key": f"remediation-{label}-{index}"},
                )
            _mark(f"bounded traffic sent ({args.traffic} checkouts)")

            starts_at = datetime.now(UTC).isoformat()
            run_fingerprint = f"remediation-{label}-{int(time.time())}"
            webhook = {
                "version": "4",
                "status": "firing",
                "receiver": "incident-api",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "DemoPaymentBadDeployment",
                            "service": "payment-service",
                            "severity": "warning",
                            "run": run_fingerprint,
                        },
                        "annotations": {
                            "summary": "Synthetic firing alert: no per-fault rule yet (ADR-0010)"
                        },
                        "startsAt": starts_at,
                        "endsAt": "0001-01-01T00:00:00Z",
                        "generatorURL": "http://prometheus:9090/graph",
                        "fingerprint": run_fingerprint,
                    }
                ],
            }
            accepted = _check(
                "alert ingest",
                await client.post(f"{INCIDENT_API}/api/v1/alerts", json=webhook),
                expect=202,
            )
            incident_id = str(accepted["alerts"][0]["incident_id"])
            _mark(f"incident {incident_id} ingested")

            jobs = _check(
                "jobs",
                await client.get(
                    f"{INCIDENT_API}/api/v1/investigation-jobs",
                    params={"incident_id": incident_id},
                ),
            )
            items = jobs.get("items", [])
            if not items:
                raise RuntimeError("no investigation job for incident")
            job_id = UUID(str(items[0]["id"]))

            claim = await incident_store.claim_job(job_id, incident_id)
            if not claim.claimed:
                raise RuntimeError(f"script could not claim the evidence job: {claim.reason}")
            telemetry = TelemetryRuntime.create(service_name="scenario", settings=settings)
            collector = EvidenceCollectionService(
                store=evidence_store,
                adapters=EvidenceAdapters(
                    prometheus=PrometheusAdapter(PrometheusClient(settings)),
                    loki=LokiAdapter(LokiClient(settings)),
                    tempo=TempoAdapter(TempoClient(settings)),
                    deployments=DeploymentAdapter(DeploymentClient(evidence_store)),
                ),
                settings=settings,
                telemetry=telemetry,
            )
            # Tempo/Loki arrive over async OTLP export; re-collect (monotonic
            # upsert keeps collected rows) until slow traces land or we time out.
            # The job is completed exactly once via complete_ai_job below,
            # mirroring the worker's AI path.
            required = {"metric.service_latency_p95", "trace.slow_service"}
            evidence_deadline = time.perf_counter() + 120.0
            collected = await evidence_store.all_evidence(incident_id)
            while time.perf_counter() < evidence_deadline and not required.issubset(
                {str(item.query_template) for item in collected if str(item.status) == "collected"}
            ):
                await asyncio.sleep(5.0)
                await collector.collect(claim)
                collected = await evidence_store.all_evidence(incident_id)
            by_template = {
                str(item.query_template): item.id
                for item in collected
                if str(item.status) == "collected"
            }
            metric_id = by_template.get("metric.service_latency_p95")
            log_id = next(
                (
                    item.id
                    for item in collected
                    if str(item.status) == "collected" and str(item.source) == "loki"
                ),
                None,
            )
            trace_id = next(
                (
                    item.id
                    for item in collected
                    if str(item.status) == "collected" and str(item.source) == "tempo"
                ),
                None,
            )
            deploy_id = next(
                (
                    item.id
                    for item in collected
                    if str(item.status) == "collected"
                    and str(item.query_template) == "deployment.current_previous"
                ),
                None,
            )
            if None in (metric_id, log_id, trace_id, deploy_id):
                raise RuntimeError(f"insufficient live evidence: {sorted(by_template)}")
            assert metric_id and log_id and trace_id and deploy_id
            _mark(f"evidence collected ({len(collected)} items, production collector)")

            description = "Payment latency regressed after the 0.2.0 deployment"
            primary = HypothesisCandidate(
                category=RootCauseCategory.BAD_DEPLOYMENT,
                description=description,
                initial_evidence_ids=[deploy_id, metric_id],
            )
            script = {
                "HypothesisCandidates": [
                    HypothesisCandidates(
                        hypotheses=[
                            primary,
                            HypothesisCandidate(
                                category=RootCauseCategory.DATABASE_LATENCY,
                                description="Payment persistence slowed by query latency",
                                initial_evidence_ids=[metric_id],
                            ),
                            HypothesisCandidate(
                                category=RootCauseCategory.CPU_SATURATION,
                                description="Payment CPU pressure slows responses",
                                initial_evidence_ids=[trace_id],
                            ),
                        ]
                    )
                ],
                "HypothesisVerification": [
                    HypothesisVerification(
                        status=HypothesisStatus.VERIFIED,
                        confidence=0.8,
                        supporting_evidence_ids=[deploy_id, metric_id],
                        contradicting_evidence_ids=[],
                        reasoning_summary="Version change precedes latency elevation",
                    ),
                    HypothesisVerification(
                        status=HypothesisStatus.VERIFIED,
                        confidence=0.6,
                        supporting_evidence_ids=[metric_id],
                        contradicting_evidence_ids=[log_id],
                        reasoning_summary="Logs contradict pure persistence delay",
                    ),
                    HypothesisVerification(
                        status=HypothesisStatus.INCONCLUSIVE,
                        confidence=0.2,
                        supporting_evidence_ids=[],
                        contradicting_evidence_ids=[],
                        reasoning_summary="No CPU signal collected",
                    ),
                ],
                "ReportSynthesis": [
                    ReportSynthesis(
                        selected_hypothesis_id=stable_hypothesis_id(
                            incident_id, RootCauseCategory.BAD_DEPLOYMENT, description
                        ),
                        recommendations=[
                            RecommendationProposal(
                                action_type=RecommendationAction.ROLLBACK_DEPLOYMENT,
                                target=EvidenceService.PAYMENT,
                                rationale_evidence_ids=[deploy_id],
                                risk=RecommendationRisk.MEDIUM,
                                reversible=True,
                            )
                        ],
                    )
                ],
            }
            provider = _ScriptedProvider(script)
            gateway = BudgetedModelGateway(
                provider=provider, store=artifact_store, settings=settings, usage=RunUsage()
            )
            workflow = InvestigatorWorkflow(
                settings=settings,
                checkpointer=MemorySaver(),
                evidence_store=evidence_store,
                artifact_store=artifact_store,
                collector=_already_collected,  # type: ignore[arg-type]
                model_gateway=gateway,
            )
            report = await workflow.run(claim)
            await incident_store.complete_ai_job(job_id, report=report)
            if report.root_cause is None:
                raise RuntimeError("investigation returned null; cannot demonstrate rollback")
            rec_id = report.recommendations[0].id
            _mark(f"report {report.id} persisted via production complete_ai_job")

            incident = _check(
                "incident", await client.get(f"{INCIDENT_API}/api/v1/incidents/{incident_id}")
            )
            version = int(incident["version"])
            _check(
                "approve",
                await client.post(
                    f"{INCIDENT_API}/api/v1/recommendations/{rec_id}/approve",
                    json={"incident_version": version, "actor": "scenario-approver"},
                    headers={"Idempotency-Key": f"remediation-{label}-approve"},
                ),
            )
            _mark("recommendation approved")
            incident = _check(
                "incident", await client.get(f"{INCIDENT_API}/api/v1/incidents/{incident_id}")
            )
            version = int(incident["version"])

            execution = _check(
                "execute",
                await client.post(
                    f"{INCIDENT_API}/api/v1/recommendations/{rec_id}/execute",
                    json={
                        "incident_version": version,
                        "expected_service_version": BAD_VERSION,
                        "actor": "scenario-approver",
                    },
                    headers={"Idempotency-Key": f"remediation-{label}-execute"},
                ),
                expect=202,
            )
            execution_id = str(execution["execution"]["id"])
            _mark(f"execution {execution_id} claimed; starting worker")

            _compose("start", "investigator-worker")
            worker_stopped = False

            async def _resolved() -> object:
                status = await client.get(f"{INCIDENT_API}/api/v1/incidents/{incident_id}")
                if status.is_error:
                    return None
                return status.json().get("status") == "resolved" or None

            await _wait_for("incident resolved", 240.0, _resolved, interval=5.0)
            final_execution = _check(
                "execution", await client.get(f"{INCIDENT_API}/api/v1/remediations/{execution_id}")
            )
            _mark("incident RESOLVED with verified recovery")
            print(json.dumps({"execution": final_execution, "label": label}, indent=2)[:800])
    except Exception as error:
        print(f"SCENARIO FAILED: {error}", flush=True)
        return 1
    finally:
        with suppress(Exception):
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                await client.put(
                    f"{PAYMENT}/internal/faults/bad-deployment",
                    json={"enabled": False},
                    headers={"X-Fault-Control-Token": token},
                )
        if worker_stopped:
            _compose("start", "investigator-worker")
        for store in (incident_store, evidence_store, artifact_store):
            with suppress(Exception):
                await store.close()
    total = time.perf_counter() - started_all
    print(f"SCENARIO OK in {total:.1f}s (label={label})", flush=True)
    for step, at in timings.items():
        print(f"  {at:6.1f}s  {step}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
