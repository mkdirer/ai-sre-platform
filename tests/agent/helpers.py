"""Shared deterministic fakes for Stage 06 investigator tests.

No fake here performs network I/O or requires credentials. Model behavior is
scripted per structured-output type; persistence is in-memory.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from packages.agents.provider import ProviderResult, StructuredModelProvider
from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceItem,
    EvidenceSource,
    EvidenceType,
    EvidenceWindow,
    QueryTemplate,
    SourceCollectionSummary,
)
from packages.models.incidents import IncidentSeverity
from packages.models.investigation import ModelCallRecord, RunUsage
from packages.persistence import WorkerClaim

INCIDENT_ID = "INC-A1B2C3D4E5F60708"
OTHER_INCIDENT_ID = "INC-FFFFFFFFFFFFFFFF"
RUN_ID = UUID("42a9f41a-c334-4ad9-99da-0e52ae33576f")
JOB_ID = UUID("7af2ffbd-50fe-42ae-b8be-58ca28fe3f8e")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
TRACE_ID = "ab" * 16


def evd_id(n: int) -> str:
    """Return a deterministic valid evidence ID."""

    return f"EVD-{n:024X}"


def make_window() -> EvidenceWindow:
    return EvidenceWindow(start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=5))


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def make_item(
    n: int,
    *,
    incident_id: str = INCIDENT_ID,
    source: EvidenceSource = EvidenceSource.PROMETHEUS,
    type: EvidenceType = EvidenceType.METRIC,
    status: CollectionStatus = CollectionStatus.COLLECTED,
    template: QueryTemplate = QueryTemplate.METRIC_SERVICE_LATENCY,
    summary: str = "fixture evidence",
    payload: dict[str, object] | None = None,
    observed_at: datetime | None = None,
    window: EvidenceWindow | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> EvidenceItem:
    resolved_window = window or make_window()
    return EvidenceItem(
        id=evd_id(n),
        incident_id=incident_id,
        source=source,
        type=type,
        status=status,
        observed_at=observed_at or NOW,
        window=resolved_window,
        summary=summary,
        payload=dict(payload or {}),
        query_template=template,
        query_parameters={"service": "payment-service"},
        provenance={"adapter": "fixture", "attempt": 1},
        error_type=error_type,
        error_message=error_message,
        payload_sha256="ab" * 32,
        collected_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def make_failed_item(n: int, source: EvidenceSource) -> EvidenceItem:
    """Build an explicit unavailable-source item (missing data, not negative proof)."""

    return make_item(
        n,
        source=source,
        type=(EvidenceType.LOG if source == EvidenceSource.LOKI else EvidenceType.TRACE),
        status=CollectionStatus.UNAVAILABLE,
        template=(
            QueryTemplate.LOG_SERVICE_ERRORS
            if source == EvidenceSource.LOKI
            else QueryTemplate.TRACE_BY_ID
        ),
        summary=f"{source.value} backend unavailable during collection",
        payload={},
        error_type="adapter_unavailable",
        error_message=f"{source.value} backend unavailable",
    )


def make_claim(
    *,
    run_id: UUID = RUN_ID,
    incident_id: str = INCIDENT_ID,
    service: str = "payment-service",
    affected: tuple[str, ...] = ("payment-service",),
) -> WorkerClaim:
    return WorkerClaim(
        claimed=True,
        reason="claimed",
        job_id=JOB_ID,
        run_id=run_id,
        incident_id=incident_id,
        incident_title="Payment latency",
        service=service,
        affected_services=affected,
        severity=IncidentSeverity.WARNING,
        started_at=NOW,
        investigation_window_start=NOW - timedelta(minutes=10),
        investigation_window_end=NOW + timedelta(minutes=5),
        attempt=1,
        max_attempts=3,
    )


class ScriptedProvider(StructuredModelProvider):
    """Deterministic provider dispatching canned outputs by response model type."""

    def __init__(self, script: Mapping[str, Sequence[object]]) -> None:
        self._script = {key: list(value) for key, value in script.items()}
        self.calls: list[dict[str, object]] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "fake"

    async def complete(
        self,
        *,
        model: str,
        instructions: str,
        input_json: str,
        response_model: type[BaseModel],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult[BaseModel]:
        self.calls.append({"model": model, "response": response_model.__name__})
        queue = self._script[response_model.__name__]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, BaseModel)
        return ProviderResult(
            output=item,
            response_id="fake-response",
            input_tokens=10,
            output_tokens=10,
        )

    async def close(self) -> None:
        self.closed = True


class InMemoryEvidenceStore:
    """Canonical evidence reads over a fixed fixture set."""

    def __init__(self, items: Sequence[EvidenceItem]) -> None:
        self._items = tuple(items)

    async def all_evidence(self, incident_id: str) -> tuple[EvidenceItem, ...]:
        return tuple(item for item in self._items if item.incident_id == incident_id)


class InMemoryArtifactStore:
    """Durable-artifact double recording hypotheses, reports, calls, and failures."""

    def __init__(self) -> None:
        self.hypotheses: list[object] = []
        self.reports: list[object] = []
        self.calls: list[ModelCallRecord] = []
        self.failures: list[dict[str, object]] = []

    async def save_hypotheses(
        self, run_id: UUID, incident_id: str, hypotheses: Sequence[object]
    ) -> None:
        self.hypotheses.append((run_id, incident_id, tuple(hypotheses)))

    async def save_report(self, run_id: UUID, report: object) -> None:
        self.reports.append((run_id, report))

    async def record_call(self, record: ModelCallRecord) -> None:
        self.calls.append(record)

    async def usage_for_run(self, run_id: UUID) -> RunUsage:
        models = sum(1 for call in self.calls if call.kind == "model")
        tools = sum(1 for call in self.calls if call.kind == "tool")
        return RunUsage(
            model_calls=models,
            tool_calls=tools,
            input_tokens=sum(call.input_tokens or 0 for call in self.calls),
            output_tokens=sum(call.output_tokens or 0 for call in self.calls),
            estimated_cost_usd=sum(call.estimated_cost_usd or 0.0 for call in self.calls),
        )

    async def record_failure(
        self,
        *,
        failure_id: str,
        run_id: UUID,
        incident_id: str,
        stage: str,
        error: BaseException,
        details: dict[str, object] | None = None,
    ) -> None:
        self.failures.append(
            {
                "failure_id": failure_id,
                "run_id": run_id,
                "incident_id": incident_id,
                "stage": stage,
                "error_type": type(error).__name__,
            }
        )


async def empty_collector(claim: WorkerClaim) -> tuple[SourceCollectionSummary, ...]:
    """Collector double for tests where fixtures are preloaded in the evidence store."""

    return ()


def persisted_item_from_draft(n: int, incident_id: str, draft: EvidenceDraft) -> EvidenceItem:
    """Attach identity/storage metadata to an adapter draft for tool tests."""

    return EvidenceItem(
        **draft.model_dump(),
        id=evd_id(n),
        incident_id=incident_id,
        payload_sha256="cd" * 32,
        collected_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
