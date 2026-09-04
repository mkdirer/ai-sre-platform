"""Fixture-based Stage 06 investigator demonstration (no credentials, no network).

Runs the real checkpointed LangGraph workflow against canned slow-database
evidence with a deterministic scripted provider and prints the validated
IncidentReport as JSON:

    uv run python scripts/demo_investigation_report.py
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver

from packages.agents.provider import BudgetedModelGateway, ProviderResult
from packages.agents.validation import stable_hypothesis_id
from packages.agents.workflow import InvestigatorWorkflow
from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceItem,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    QueryTemplate,
)
from packages.models.incidents import IncidentSeverity
from packages.models.investigation import (
    HypothesisCandidate,
    HypothesisCandidates,
    HypothesisVerification,
    RecommendationAction,
    RecommendationProposal,
    RecommendationRisk,
    ReportSynthesis,
    RootCauseCategory,
    RunUsage,
)
from packages.persistence import WorkerClaim

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
INCIDENT_ID = "INC-A1B2C3D4E5F60708"
TRACE_ID = "ab" * 16


def _item(n: int, **overrides) -> EvidenceItem:
    values: dict = {
        "id": f"EVD-{n:024X}",
        "incident_id": INCIDENT_ID,
        "source": EvidenceSource.PROMETHEUS,
        "type": EvidenceType.METRIC,
        "status": CollectionStatus.COLLECTED,
        "observed_at": NOW,
        "window": EvidenceWindow(start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=5)),
        "summary": "fixture evidence",
        "payload": {},
        "query_template": QueryTemplate.METRIC_SERVICE_LATENCY,
        "query_parameters": {"service": "payment-service"},
        "provenance": {"adapter": "demo-fixture"},
        "payload_sha256": "ab" * 32,
        "collected_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return EvidenceItem(**values)


def _evidence() -> tuple[EvidenceItem, ...]:
    return (
        _item(
            1,
            summary="payment p95 latency 2.4s exceeds the demo threshold",
            payload={
                "observation": "slow_database delay injected before persistence",
                "duration_ms": 2400,
            },
        ),
        _item(
            2,
            source=EvidenceSource.LOKI,
            type=EvidenceType.LOG,
            query_template=QueryTemplate.LOG_GROUPED_PATTERNS,
            summary="grouped payment logs repeat the persistence delay notice",
            payload={"patterns": ["slow_database delay injected before persistence"]},
        ),
        _item(
            3,
            source=EvidenceSource.TEMPO,
            type=EvidenceType.TRACE,
            query_template=QueryTemplate.TRACE_SLOW_SERVICE,
            summary="slow payment trace dominated by the persistence span",
            payload={"trace_id": TRACE_ID, "duration_ms": 2510},
        ),
        _item(
            4,
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            query_template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
            summary="payment deployment unchanged across the window",
            payload={
                "current": {"id": "DEP-0002", "version": "0.1.0"},
                "previous": {"id": "DEP-0001", "version": "0.1.0"},
            },
        ),
    )


class _ScriptedProvider:
    """Minimal deterministic provider for the demonstration."""

    name = "demo-fixture"

    def __init__(self, script: dict[str, list]) -> None:
        self._script = script

    async def complete(
        self, *, model, instructions, input_json, response_model, **kwargs
    ) -> ProviderResult:
        item = self._script[response_model.__name__].pop(0)
        return ProviderResult(output=item, input_tokens=10, output_tokens=10)

    async def close(self) -> None:
        pass


class _Stores:
    def __init__(self, evidence: tuple[EvidenceItem, ...]) -> None:
        self._evidence = evidence
        self.calls = 0

    async def all_evidence(self, incident_id: str) -> tuple[EvidenceItem, ...]:
        return tuple(item for item in self._evidence if item.incident_id == incident_id)

    async def save_hypotheses(self, run_id, incident_id, hypotheses) -> None:
        pass

    async def save_report(self, run_id, report) -> None:
        pass

    async def record_call(self, record) -> None:
        self.calls += 1

    async def usage_for_run(self, run_id) -> RunUsage:
        return RunUsage()

    async def record_failure(self, **kwargs) -> None:
        print(f"failure: {kwargs}")


async def _run() -> None:
    description = "Payment persistence slowed by the injected database delay"
    selected = stable_hypothesis_id(INCIDENT_ID, RootCauseCategory.DATABASE_LATENCY, description)
    evidence = _evidence()
    provider = _ScriptedProvider(
        {
            "HypothesisCandidates": [
                HypothesisCandidates(
                    hypotheses=[
                        HypothesisCandidate(
                            category=RootCauseCategory.DATABASE_LATENCY,
                            description=description,
                            initial_evidence_ids=[evidence[0].id, evidence[2].id],
                        ),
                        HypothesisCandidate(
                            category=RootCauseCategory.BAD_DEPLOYMENT,
                            description="A recent payment deployment regressed latency",
                            initial_evidence_ids=[evidence[3].id],
                        ),
                        HypothesisCandidate(
                            category=RootCauseCategory.UPSTREAM_TIMEOUT,
                            description="Inventory upstream timeouts backpressured payment",
                            initial_evidence_ids=[evidence[0].id],
                        ),
                    ]
                )
            ],
            "HypothesisVerification": [
                HypothesisVerification(
                    status="verified",
                    confidence=0.8,
                    supporting_evidence_ids=[evidence[0].id, evidence[2].id],
                    contradicting_evidence_ids=[],
                    reasoning_summary="Latency and spans coincide with the delay notice",
                ),
                HypothesisVerification(
                    status="verified",
                    confidence=0.7,
                    supporting_evidence_ids=[evidence[3].id],
                    contradicting_evidence_ids=[],
                    reasoning_summary="Versions are identical across the window",
                ),
                HypothesisVerification(
                    status="verified",
                    confidence=0.6,
                    supporting_evidence_ids=[evidence[0].id],
                    contradicting_evidence_ids=[evidence[1].id],
                    reasoning_summary="Local delay evidence contradicts an upstream cause",
                ),
            ],
            "ReportSynthesis": [
                ReportSynthesis(
                    selected_hypothesis_id=selected,
                    recommendations=[
                        RecommendationProposal(
                            action_type=RecommendationAction.INVESTIGATE_DATABASE,
                            target="payment-service",
                            rationale_evidence_ids=[evidence[0].id],
                            risk=RecommendationRisk.LOW,
                            reversible=True,
                        )
                    ],
                )
            ],
        }
    )
    settings = Settings(_env_file=None)
    stores = _Stores(evidence)
    gateway = BudgetedModelGateway(
        provider=provider,
        store=stores,
        settings=settings,
        usage=RunUsage(),  # type: ignore[arg-type]
    )
    workflow = InvestigatorWorkflow(
        settings=settings,
        checkpointer=MemorySaver(),
        evidence_store=stores,
        artifact_store=stores,
        collector=lambda _claim: asyncio.sleep(0, result=()),
        model_gateway=gateway,
    )
    run_id = uuid4()
    claim = WorkerClaim(
        claimed=True,
        reason="demo",
        job_id=uuid4(),
        run_id=run_id,
        incident_id=INCIDENT_ID,
        incident_title="Payment latency",
        service="payment-service",
        affected_services=("payment-service",),
        severity=IncidentSeverity.WARNING,
        started_at=NOW,
        investigation_window_start=NOW - timedelta(minutes=10),
        investigation_window_end=NOW + timedelta(minutes=5),
        attempt=1,
        max_attempts=3,
    )
    report = await workflow.run(claim)
    output: dict = json.loads(report.model_dump_json())
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_run())
