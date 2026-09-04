"""Closed additional-evidence registry whose parameters derive from canonical anchors."""

import hashlib
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from packages.config import Settings
from packages.models.evidence import (
    CollectionStatus,
    EvidenceDraft,
    EvidenceItem,
    EvidenceService,
    EvidenceWindow,
    LogsAroundQuery,
    TraceByIdQuery,
)
from packages.models.investigation import (
    AdditionalEvidenceKind,
    AdditionalEvidenceRequest,
    ModelCallRecord,
)
from packages.telemetry import TelemetryRuntime, redact_text
from packages.tools.loki import LokiAdapter
from packages.tools.tempo import TempoAdapter


class AdditionalEvidenceRejected(ValueError):
    """A request could not be resolved entirely from incident-owned evidence."""


class EvidenceToolStore(Protocol):
    """Persistence boundary for additional evidence and tool-call audit."""

    async def persist_evidence(
        self, incident_id: str, drafts: Sequence[EvidenceDraft]
    ) -> tuple[EvidenceItem, ...]: ...


class ToolCallStore(Protocol):
    async def record_call(self, record: ModelCallRecord) -> None: ...


class AdditionalEvidenceTools:
    """Execute only two anchor-derived, read-only domain operations."""

    def __init__(
        self,
        *,
        loki: LokiAdapter,
        tempo: TempoAdapter,
        evidence_store: EvidenceToolStore,
        call_store: ToolCallStore,
        settings: Settings,
        telemetry: TelemetryRuntime | None = None,
    ) -> None:
        self._loki = loki
        self._tempo = tempo
        self._evidence_store = evidence_store
        self._call_store = call_store
        self._settings = settings
        self._telemetry = telemetry

    async def execute(
        self,
        request: AdditionalEvidenceRequest,
        *,
        run_id: UUID,
        incident_id: str,
        scope_services: set[EvidenceService],
        window: EvidenceWindow,
        evidence: dict[str, EvidenceItem],
        iteration: int,
    ) -> EvidenceItem | None:
        """Resolve safe parameters, execute one allowlisted method, and persist its result."""

        logical_key = (
            f"{request.kind.value}:{request.service.value}:{request.anchor_evidence_id}:{iteration}"
        )
        call_id = _tool_call_id(run_id, logical_key)
        started = time.perf_counter()
        try:
            if request.service not in scope_services:
                raise AdditionalEvidenceRejected("requested service is outside incident scope")
            anchor = evidence.get(request.anchor_evidence_id)
            if anchor is None or anchor.incident_id != incident_id:
                raise AdditionalEvidenceRejected("anchor evidence does not belong to the incident")
            if request.kind == AdditionalEvidenceKind.LOGS_AROUND_EVIDENCE:
                if not window.start <= anchor.observed_at <= window.end:
                    raise AdditionalEvidenceRejected(
                        "anchor timestamp is outside the incident window"
                    )
                draft = await self._loki.get_logs_around(
                    LogsAroundQuery(
                        service=request.service,
                        window=window,
                        limit=self._settings.evidence_log_limit,
                        timestamp=anchor.observed_at,
                        radius_seconds=min(120, int((window.end - window.start).total_seconds())),
                    )
                )
            elif request.kind == AdditionalEvidenceKind.TRACE_BY_ID_FROM_EVIDENCE:
                trace_id = _find_trace_id(anchor.payload)
                if trace_id is None:
                    raise AdditionalEvidenceRejected(
                        "anchor evidence contains no canonical trace ID"
                    )
                draft = await self._tempo.get_trace_by_id(
                    TraceByIdQuery(
                        service=request.service,
                        window=window,
                        limit=self._settings.evidence_trace_limit,
                        trace_id=trace_id,
                    )
                )
            else:
                raise AdditionalEvidenceRejected("unknown additional evidence operation")
            persisted = await self._evidence_store.persist_evidence(incident_id, [draft])
            result = persisted[0] if persisted else None
            duration = time.perf_counter() - started
            await self._record(
                call_id=call_id,
                run_id=run_id,
                incident_id=incident_id,
                request=request,
                status="succeeded",
                duration=duration,
                metadata={
                    "evidence_id": result.id if result is not None else None,
                    "collection_status": draft.status.value,
                },
            )
            self._observe(request, "succeeded", duration)
            return result
        except Exception as error:
            duration = time.perf_counter() - started
            await self._record(
                call_id=call_id,
                run_id=run_id,
                incident_id=incident_id,
                request=request,
                status=("rejected" if isinstance(error, AdditionalEvidenceRejected) else "failed"),
                duration=duration,
                error=error,
            )
            self._observe(
                request,
                "rejected" if isinstance(error, AdditionalEvidenceRejected) else "failed",
                duration,
            )
            return None

    async def _record(
        self,
        *,
        call_id: str,
        run_id: UUID,
        incident_id: str,
        request: AdditionalEvidenceRequest,
        status: str,
        duration: float,
        metadata: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        await self._call_store.record_call(
            ModelCallRecord(
                id=call_id,
                run_id=str(run_id),
                incident_id=incident_id,
                kind="tool",
                operation=request.kind.value,
                status=status,
                attempt=1,
                duration_seconds=duration,
                error_type=type(error).__name__ if error is not None else None,
                error_message=(redact_text(str(error))[:512] if error is not None else None),
                metadata=metadata or {"anchor_evidence_id": request.anchor_evidence_id},
                created_at=datetime.now(UTC),
            )
        )

    def _observe(self, request: AdditionalEvidenceRequest, outcome: str, duration: float) -> None:
        if self._telemetry is not None:
            self._telemetry.metrics.observe_agent_tool_call(
                tool=request.kind.value,
                outcome=outcome,
                duration_seconds=duration,
            )


async def record_collected_evidence_calls(
    *,
    run_id: UUID,
    incident_id: str,
    evidence: Sequence[EvidenceItem],
    store: ToolCallStore,
) -> None:
    """Make the initial deterministic adapter operations durable tool-call metadata."""

    for item in evidence:
        call_id = _tool_call_id(run_id, f"initial:{item.id}")
        failed = item.status in {
            CollectionStatus.UNAVAILABLE,
            CollectionStatus.FAILED,
            CollectionStatus.TIMED_OUT,
        }
        await store.record_call(
            ModelCallRecord(
                id=call_id,
                run_id=str(run_id),
                incident_id=incident_id,
                kind="tool",
                operation=item.query_template.value,
                status="failed" if failed else "succeeded",
                attempt=1,
                duration_seconds=0,
                error_type=item.error_type,
                error_message=item.error_message,
                metadata={"evidence_id": item.id, "collection_status": item.status.value},
                created_at=item.collected_at,
            )
        )


def _tool_call_id(run_id: UUID, logical_key: str) -> str:
    digest = hashlib.sha256(f"{run_id}:tool:{logical_key}".encode()).hexdigest()
    return f"CALL-{digest[:24].upper()}"


def _find_trace_id(value: object, *, depth: int = 0) -> str | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        candidate = value.get("trace_id")
        if isinstance(candidate, str) and len(candidate) == 32:
            try:
                int(candidate, 16)
            except ValueError:
                pass
            else:
                return candidate.casefold()
        for nested in value.values():
            if result := _find_trace_id(nested, depth=depth + 1):
                return result
    elif isinstance(value, list):
        for nested in value[:100]:
            if result := _find_trace_id(nested, depth=depth + 1):
                return result
    return None
