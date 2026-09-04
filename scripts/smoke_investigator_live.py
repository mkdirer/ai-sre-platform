"""Manually invoked live-model investigator smoke test.

This script spends real money against the OpenAI Responses API. It never runs
in CI and refuses to run without explicit local opt-in:

    RUN_LIVE_INVESTIGATOR_SMOKE=true OPENAI_API_KEY=... \
        uv run python scripts/smoke_investigator_live.py

It drives the real InvestigatorWorkflow with fixture evidence, tight budgets,
and an in-memory checkpointer, then prints the validated report summary.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

OPT_IN = os.environ.get("RUN_LIVE_INVESTIGATOR_SMOKE") == "true"
API_KEY = os.environ.get("OPENAI_API_KEY", "")


def main() -> int:
    if not OPT_IN or not API_KEY:
        print(
            "skip: set RUN_LIVE_INVESTIGATOR_SMOKE=true and OPENAI_API_KEY to run "
            "the live-model investigator smoke test"
        )
        return 0
    asyncio.run(_run())
    return 0


async def _run() -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from pydantic import SecretStr

    from packages.agents.provider import BudgetedModelGateway, OpenAIResponsesProvider
    from packages.agents.workflow import InvestigatorWorkflow
    from packages.config import Settings
    from packages.models.evidence import (
        CollectionStatus,
        EvidenceItem,
        EvidenceService,
        EvidenceSource,
        EvidenceType,
        EvidenceWindow,
        QueryTemplate,
    )
    from packages.models.incidents import IncidentSeverity
    from packages.models.investigation import ModelCallRecord, RunUsage
    from packages.persistence import WorkerClaim

    now = datetime.now(UTC)
    window = EvidenceWindow(start=now - timedelta(minutes=10), end=now + timedelta(minutes=5))
    incident_id = "INC-A1B2C3D4E5F60708"
    run_id = uuid4()

    def item(n: int, **overrides) -> EvidenceItem:
        values = {
            "id": f"EVD-{n:024X}",
            "incident_id": incident_id,
            "source": EvidenceSource.PROMETHEUS,
            "type": EvidenceType.METRIC,
            "status": CollectionStatus.COLLECTED,
            "observed_at": now,
            "window": window,
            "summary": "payment p95 latency elevated with persistence delay marker",
            "payload": {"observation": "slow_database delay", "duration_ms": 2300},
            "query_template": QueryTemplate.METRIC_SERVICE_LATENCY,
            "query_parameters": {"service": "payment-service"},
            "provenance": {"adapter": "live-smoke-fixture"},
            "payload_sha256": "ab" * 32,
            "collected_at": now,
            "created_at": now,
            "updated_at": now,
        }
        values.update(overrides)
        return EvidenceItem(**values)

    evidence = (item(1),)

    class Stores:
        def __init__(self) -> None:
            self.calls: list[ModelCallRecord] = []
            self.usage = RunUsage()

        async def all_evidence(self, wanted: str):
            return tuple(item for item in evidence if item.incident_id == wanted)

        async def save_hypotheses(self, run_id, incident_id, hypotheses) -> None:
            print(f"saved {len(hypotheses)} hypotheses")

        async def save_report(self, run_id, report) -> None:
            print(f"saved report {report.id} status={report.status.value}")

        async def record_call(self, record: ModelCallRecord) -> None:
            self.calls.append(record)

        async def usage_for_run(self, run_id) -> RunUsage:
            return self.usage

        async def record_failure(self, **kwargs) -> None:
            print(f"failure recorded: {kwargs['stage']}/{kwargs['error']}")

    stores = Stores()
    settings = Settings(
        _env_file=None,
        openai_api_key=SecretStr(API_KEY),
        investigator_max_iterations=1,
        investigator_max_model_calls=8,
        investigator_max_output_tokens_per_call=512,
        investigator_max_duration_seconds=180.0,
    )
    provider = OpenAIResponsesProvider(settings)
    try:
        gateway = BudgetedModelGateway(
            provider=provider, store=stores, settings=settings, usage=RunUsage()
        )
        workflow = InvestigatorWorkflow(
            settings=settings,
            checkpointer=MemorySaver(),
            evidence_store=stores,
            artifact_store=stores,
            collector=lambda _claim: asyncio.sleep(0, result=()),
            model_gateway=gateway,
        )
        claim = WorkerClaim(
            claimed=True,
            reason="manual-smoke",
            job_id=uuid4(),
            run_id=run_id,
            incident_id=incident_id,
            incident_title="Payment latency",
            service="payment-service",
            affected_services=("payment-service",),
            severity=IncidentSeverity.WARNING,
            started_at=now,
            investigation_window_start=window.start,
            investigation_window_end=window.end,
            attempt=1,
            max_attempts=1,
        )
        report = await workflow.run(claim)
    finally:
        await provider.close()

    print(f"root_cause={report.root_cause} confidence={report.confidence}")
    print(f"status={report.status.value} hypotheses={len(report.hypotheses)}")
    print(
        f"model_calls={gateway.usage.model_calls} "
        f"tokens={gateway.usage.input_tokens + gateway.usage.output_tokens}"
    )
    print(f"services={[service.value for service in (EvidenceService.PAYMENT,)]}")


if __name__ == "__main__":
    raise SystemExit(main())
