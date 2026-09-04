"""Allowlist and anchor-ownership tests for bounded additional evidence tools."""

from datetime import timedelta
from uuid import UUID

import pytest

from packages.agents.evidence_tools import (
    AdditionalEvidenceTools,
    record_collected_evidence_calls,
)
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
)
from packages.models.investigation import AdditionalEvidenceKind, AdditionalEvidenceRequest
from tests.agent.helpers import (
    INCIDENT_ID,
    NOW,
    OTHER_INCIDENT_ID,
    RUN_ID,
    TRACE_ID,
    InMemoryArtifactStore,
    evd_id,
    make_item,
    make_settings,
    make_window,
    persisted_item_from_draft,
)

METRIC_ID = evd_id(1)


class _StubLoki:
    def __init__(self, draft: EvidenceDraft) -> None:
        self._draft = draft
        self.queries: list = []

    async def get_logs_around(self, query) -> EvidenceDraft:
        self.queries.append(query)
        return self._draft


class _StubTempo:
    def __init__(self, draft: EvidenceDraft) -> None:
        self._draft = draft
        self.queries: list = []

    async def get_trace_by_id(self, query) -> EvidenceDraft:
        self.queries.append(query)
        return self._draft


class _PersistStore:
    def __init__(self, incident_id: str = INCIDENT_ID) -> None:
        self.incident_id = incident_id
        self.persisted: list = []

    async def persist_evidence(self, incident_id: str, drafts) -> tuple:
        items = tuple(
            persisted_item_from_draft(20 + index, incident_id, draft)
            for index, draft in enumerate(drafts)
        )
        self.persisted.extend(items)
        return items


def _logs_draft() -> EvidenceDraft:
    window = make_window()
    return EvidenceDraft(
        source=EvidenceSource.LOKI,
        type=EvidenceType.LOG,
        status=CollectionStatus.COLLECTED,
        observed_at=NOW,
        window=window,
        summary="context logs around the anchor",
        payload={"lines": ["slow_database delay injected before persistence"]},
        query_template=QueryTemplate.LOG_AROUND_TIMESTAMP,
        query_parameters={"service": "payment-service"},
        provenance={"adapter": "fixture"},
    )


def _trace_draft() -> EvidenceDraft:
    window = make_window()
    return EvidenceDraft(
        source=EvidenceSource.TEMPO,
        type=EvidenceType.TRACE,
        status=CollectionStatus.COLLECTED,
        observed_at=NOW,
        window=window,
        summary="trace resolved from anchor",
        payload={"trace_id": TRACE_ID},
        query_template=QueryTemplate.TRACE_BY_ID,
        query_parameters={"service": "payment-service"},
        provenance={"adapter": "fixture"},
    )


def _tools(
    loki_draft: EvidenceDraft | None = None,
    tempo_draft: EvidenceDraft | None = None,
) -> tuple[AdditionalEvidenceTools, _StubLoki, _StubTempo, _PersistStore, InMemoryArtifactStore]:
    loki = _StubLoki(loki_draft or _logs_draft())
    tempo = _StubTempo(tempo_draft or _trace_draft())
    persist = _PersistStore()
    calls = InMemoryArtifactStore()
    tools = AdditionalEvidenceTools(
        loki=loki,  # type: ignore[arg-type]
        tempo=tempo,  # type: ignore[arg-type]
        evidence_store=persist,
        call_store=calls,
        settings=make_settings(),
    )
    return tools, loki, tempo, persist, calls


def _anchor(payload: dict | None = None):
    return make_item(1, summary="anchor metric", payload=payload or {"duration_ms": 2400})


def _request(**overrides) -> AdditionalEvidenceRequest:
    values = {
        "kind": AdditionalEvidenceKind.LOGS_AROUND_EVIDENCE,
        "service": EvidenceService.PAYMENT,
        "anchor_evidence_id": METRIC_ID,
        "reason": "need wider log context",
    }
    values.update(overrides)
    return AdditionalEvidenceRequest(**values)


@pytest.mark.asyncio
async def test_logs_around_anchor_succeeds_and_persists() -> None:
    """A well-scoped anchor request executes exactly one allowlisted operation."""

    tools, loki, _, persist, calls = _tools()

    result = await tools.execute(
        _request(),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: _anchor()},
        iteration=1,
    )

    assert result is not None
    assert result.incident_id == INCIDENT_ID
    assert len(loki.queries) == 1
    assert persist.persisted
    (record,) = calls.calls
    assert record.status == "succeeded"
    assert record.metadata["evidence_id"] == result.id


@pytest.mark.asyncio
async def test_out_of_scope_service_is_rejected_without_adapter_call() -> None:
    """The model cannot pivot to services outside the incident scope."""

    tools, loki, tempo, _, calls = _tools()

    result = await tools.execute(
        _request(service=EvidenceService.INVENTORY),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: _anchor()},
        iteration=1,
    )

    assert result is None
    assert loki.queries == [] and tempo.queries == []
    (record,) = calls.calls
    assert record.status == "rejected"


@pytest.mark.asyncio
async def test_foreign_anchor_is_rejected() -> None:
    """Anchors must belong to the incident under investigation."""

    tools, loki, _, _, calls = _tools()
    foreign_anchor = make_item(1, incident_id=OTHER_INCIDENT_ID)

    result = await tools.execute(
        _request(),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: foreign_anchor},
        iteration=1,
    )

    assert result is None
    assert loki.queries == []
    assert calls.calls[0].status == "rejected"


@pytest.mark.asyncio
async def test_trace_request_without_trace_id_is_rejected() -> None:
    """Trace lookups require a canonical trace ID inside the anchor payload."""

    tools, _, tempo, _, calls = _tools()

    result = await tools.execute(
        _request(
            kind=AdditionalEvidenceKind.TRACE_BY_ID_FROM_EVIDENCE,
            anchor_evidence_id=METRIC_ID,
        ),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: _anchor(payload={"note": "no trace here"})},
        iteration=1,
    )

    assert result is None
    assert tempo.queries == []
    assert calls.calls[0].status == "rejected"


@pytest.mark.asyncio
async def test_trace_request_resolves_anchor_trace_id() -> None:
    """A canonical trace ID in the anchor selects the trace operation safely."""

    tools, _, tempo, _, calls = _tools()

    result = await tools.execute(
        _request(kind=AdditionalEvidenceKind.TRACE_BY_ID_FROM_EVIDENCE),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: _anchor(payload={"trace_id": TRACE_ID})},
        iteration=1,
    )

    assert result is not None
    assert len(tempo.queries) == 1
    assert tempo.queries[0].trace_id == TRACE_ID
    assert calls.calls[0].status == "succeeded"


@pytest.mark.asyncio
async def test_anchor_outside_window_is_rejected() -> None:
    """Log-context anchors must fall inside the incident window."""

    tools, loki, _, _, calls = _tools()
    stale = make_item(1, observed_at=NOW - timedelta(hours=2))

    result = await tools.execute(
        _request(),
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        scope_services={EvidenceService.PAYMENT},
        window=make_window(),
        evidence={METRIC_ID: stale},
        iteration=1,
    )

    assert result is None
    assert loki.queries == []
    assert calls.calls[0].status == "rejected"


@pytest.mark.asyncio
async def test_initial_collection_calls_become_tool_audit() -> None:
    """Deterministic adapter results are durable tool-call metadata."""

    from packages.models.evidence import CollectionStatus as Status

    calls = InMemoryArtifactStore()
    await record_collected_evidence_calls(
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        evidence=[
            make_item(1),
            make_item(
                5,
                source=EvidenceSource.LOKI,
                type=EvidenceType.LOG,
                template=QueryTemplate.LOG_SERVICE_ERRORS,
                status=Status.FAILED,
                summary="loki failed",
                payload={},
                error_type="adapter_error",
                error_message="boom",
            ),
        ],
        store=calls,
    )

    assert [call.status for call in calls.calls] == ["succeeded", "failed"]
    assert calls.calls[0].metadata["evidence_id"] == evd_id(1)
    assert UUID(calls.calls[0].run_id) == RUN_ID
