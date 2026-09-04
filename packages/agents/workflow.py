"""Typed, checkpointed, evidence-grounded LangGraph investigation workflow."""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from packages.agents.evidence_tools import (
    AdditionalEvidenceTools,
    record_collected_evidence_calls,
)
from packages.agents.prompts import (
    BASE_INSTRUCTIONS,
    GENERATE_HYPOTHESES_INSTRUCTIONS,
    SYNTHESIZE_REPORT_INSTRUCTIONS,
    VERIFY_HYPOTHESIS_INSTRUCTIONS,
)
from packages.agents.provider import BudgetedModelGateway, InvestigatorBudgetExceeded
from packages.agents.validation import (
    build_report,
    canonicalize_verification,
    eligible_hypotheses,
    validate_candidates,
)
from packages.config import Settings
from packages.incidents.timeline import correlate_timeline
from packages.models.evidence import (
    EvidenceItem,
    EvidenceService,
    EvidenceTimelinePage,
    EvidenceWindow,
    SourceCollectionSummary,
)
from packages.models.investigation import (
    AdditionalEvidenceRequest,
    Hypothesis,
    HypothesisCandidate,
    HypothesisCandidates,
    HypothesisVerification,
    IncidentReport,
    ModelOperation,
    ReportSynthesis,
    RunUsage,
)
from packages.persistence import WorkerClaim
from packages.telemetry import TelemetryRuntime, redact_value


class InvestigatorState(TypedDict, total=False):
    """JSON-serializable graph state persisted by the configured checkpointer."""

    run_id: str
    incident_id: str
    title: str
    service: str
    affected_services: list[str]
    severity: str
    window: dict[str, str]
    plan: dict[str, object]
    source_summaries: list[dict[str, object]]
    initial_collection_complete: bool
    evidence: list[dict[str, object]]
    timeline: list[dict[str, object]]
    candidates: list[dict[str, object]]
    hypotheses: list[dict[str, object]]
    eligible_hypothesis_ids: list[str]
    pending_requests: list[dict[str, object]]
    completed_request_keys: list[str]
    iteration: int
    usage: dict[str, object]
    report: dict[str, object]


class EvidenceRepository(Protocol):
    """Canonical evidence reads required by the workflow."""

    async def all_evidence(self, incident_id: str) -> tuple[EvidenceItem, ...]: ...


class InvestigationArtifactStore(Protocol):
    """Durable investigator artifact operations used inside retry-safe nodes."""

    async def save_hypotheses(
        self, run_id: UUID, incident_id: str, hypotheses: Sequence[Hypothesis]
    ) -> None: ...

    async def save_report(self, run_id: UUID, report: IncidentReport) -> None: ...

    async def usage_for_run(self, run_id: UUID) -> RunUsage: ...

    async def record_failure(
        self,
        *,
        failure_id: str,
        run_id: UUID,
        incident_id: str,
        stage: str,
        error: BaseException,
        details: dict[str, object] | None = None,
    ) -> None: ...


EvidenceCollector = Callable[[WorkerClaim], Awaitable[tuple[SourceCollectionSummary, ...]]]


class InvestigatorWorkflow:
    """Compile and run the bounded Stage 06 graph with durable checkpoint resume."""

    def __init__(
        self,
        *,
        settings: Settings,
        checkpointer: BaseCheckpointSaver[Any],
        evidence_store: EvidenceRepository,
        artifact_store: InvestigationArtifactStore,
        collector: EvidenceCollector,
        model_gateway: BudgetedModelGateway,
        additional_tools: AdditionalEvidenceTools | None = None,
        telemetry: TelemetryRuntime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._checkpointer = checkpointer
        self._evidence_store = evidence_store
        self._artifact_store = artifact_store
        self._collector = collector
        self._gateway = model_gateway
        self._additional_tools = additional_tools
        self._telemetry = telemetry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._claim: WorkerClaim | None = None
        builder = StateGraph(InvestigatorState)
        builder.add_node("scope_plan", self._scope_plan)
        builder.add_node("collect_or_load_evidence", self._collect_or_load_evidence)
        builder.add_node("correlate_timeline", self._correlate_timeline)
        builder.add_node("generate_hypotheses", self._generate_hypotheses)
        builder.add_node("verify_hypotheses", self._verify_hypotheses)
        builder.add_node("collect_additional_evidence", self._collect_additional_evidence)
        builder.add_node("validate_sufficiency", self._validate_sufficiency)
        builder.add_node("generate_report", self._generate_report)
        builder.add_edge(START, "scope_plan")
        builder.add_edge("scope_plan", "collect_or_load_evidence")
        builder.add_edge("collect_or_load_evidence", "correlate_timeline")
        builder.add_edge("correlate_timeline", "generate_hypotheses")
        builder.add_edge("generate_hypotheses", "verify_hypotheses")
        builder.add_conditional_edges(
            "verify_hypotheses",
            self._after_verification,
            {
                "additional": "collect_additional_evidence",
                "sufficiency": "validate_sufficiency",
            },
        )
        builder.add_edge("collect_additional_evidence", "correlate_timeline")
        builder.add_edge("validate_sufficiency", "generate_report")
        builder.add_edge("generate_report", END)
        self._graph = builder.compile(checkpointer=checkpointer, name="incident-investigator")

    async def run(self, claim: WorkerClaim) -> IncidentReport:
        """Run or resume one incident/run thread within a wall-clock deadline."""

        self._claim = claim
        started = time.perf_counter()
        config: RunnableConfig = {
            "configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""},
            "recursion_limit": self._settings.investigator_max_iterations * 4 + 12,
        }
        existing = await self._checkpointer.aget(config)
        graph_input: InvestigatorState | None = (
            None if existing is not None else _initial_state(claim)
        )
        try:
            async with asyncio.timeout(self._settings.investigator_max_duration_seconds):
                # Pregel state I/O is dynamically typed; keep the boundary explicit.
                graph_result = await self._graph.ainvoke(
                    cast(Any, graph_input),
                    config,
                    durability="sync",
                )
                result = cast(InvestigatorState, graph_result)
            report_value = result.get("report")
            if report_value is None:
                raise RuntimeError("investigator graph ended without a report")
            report = IncidentReport.model_validate(report_value)
            self._observe_workflow(report, result, time.perf_counter() - started, "succeeded")
            return report
        except BaseException as error:
            failure_digest = hashlib.sha256(
                f"{claim.run_id}:{type(error).__name__}".encode()
            ).hexdigest()
            await self._artifact_store.record_failure(
                failure_id=f"FAIL-{failure_digest[:24].upper()}",
                run_id=claim.run_id,
                incident_id=claim.incident_id,
                stage="ai_investigation",
                error=error,
            )
            self._observe_failure(time.perf_counter() - started)
            raise
        finally:
            self._claim = None

    async def _scope_plan(self, state: InvestigatorState) -> InvestigatorState:
        services = [EvidenceService(value) for value in state["affected_services"]]
        if EvidenceService(state["service"]) not in services:
            raise ValueError("primary incident service is not in affected services")
        window = EvidenceWindow.model_validate(state["window"])
        return {
            "plan": {
                "services": [service.value for service in services],
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "sources": ["prometheus", "loki", "tempo", "deployment_store"],
                "allowed_additional_tools": [
                    "logs_around_evidence",
                    "trace_by_id_from_evidence",
                ],
            },
            "iteration": state.get("iteration", 0),
            "completed_request_keys": state.get("completed_request_keys", []),
        }

    async def _collect_or_load_evidence(self, state: InvestigatorState) -> InvestigatorState:
        claim = self._require_claim()
        summaries: tuple[SourceCollectionSummary, ...] = ()
        if not state.get("initial_collection_complete", False):
            summaries = await self._collector(claim)
        evidence = await self._load_current_evidence(state)
        await record_collected_evidence_calls(
            run_id=claim.run_id,
            incident_id=claim.incident_id,
            evidence=evidence,
            store=self._gateway.call_store,
        )
        usage = await self._artifact_store.usage_for_run(claim.run_id)
        if usage.tool_calls > self._settings.investigator_max_tool_calls:
            raise InvestigatorBudgetExceeded("initial evidence plan exceeded tool-call budget")
        return {
            "initial_collection_complete": True,
            "source_summaries": [summary.model_dump(mode="json") for summary in summaries],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "usage": usage.model_dump(mode="json"),
        }

    async def _correlate_timeline(self, state: InvestigatorState) -> InvestigatorState:
        evidence = _state_evidence(state)
        page = correlate_timeline(evidence, limit=100, offset=0)
        return {"timeline": [item.model_dump(mode="json") for item in page.items]}

    async def _generate_hypotheses(self, state: InvestigatorState) -> InvestigatorState:
        iteration = state.get("iteration", 0) + 1
        output = await self._gateway.call(
            operation=ModelOperation.GENERATE_HYPOTHESES,
            response_model=HypothesisCandidates,
            instructions=BASE_INSTRUCTIONS + GENERATE_HYPOTHESES_INSTRUCTIONS,
            payload={
                "incident": _incident_context(state),
                "evidence": _bounded_evidence_context(
                    _state_evidence(state), self._settings.investigator_max_context_chars // 2
                ),
                "timeline": _bounded_timeline_context(state.get("timeline", [])),
                "iteration": iteration,
            },
            run_id=state["run_id"],
            incident_id=state["incident_id"],
            logical_key=f"iteration:{iteration}",
        )
        evidence = _state_evidence(state)
        validate_candidates(
            output.hypotheses,
            incident_id=state["incident_id"],
            affected_services=set(state["affected_services"]),
            evidence=evidence,
            minimum=self._settings.investigator_min_competing_hypotheses,
        )
        return {
            "iteration": iteration,
            "candidates": [item.model_dump(mode="json") for item in output.hypotheses],
            "usage": self._gateway.usage.model_dump(mode="json"),
        }

    async def _verify_hypotheses(self, state: InvestigatorState) -> InvestigatorState:
        evidence = _state_evidence(state)
        candidates = [HypothesisCandidate.model_validate(item) for item in state["candidates"]]
        verified: list[Hypothesis] = []
        requests: dict[str, AdditionalEvidenceRequest] = {}
        for candidate in candidates:
            stable_key = f"{candidate.category.value}:{state['iteration']}"
            output = await self._gateway.call(
                operation=ModelOperation.VERIFY_HYPOTHESIS,
                response_model=HypothesisVerification,
                instructions=BASE_INSTRUCTIONS + VERIFY_HYPOTHESIS_INSTRUCTIONS,
                payload={
                    "incident": _incident_context(state),
                    "candidate": candidate.model_dump(mode="json"),
                    "evidence": _bounded_evidence_context(
                        evidence, self._settings.investigator_max_context_chars
                    ),
                },
                run_id=state["run_id"],
                incident_id=state["incident_id"],
                logical_key=stable_key,
            )
            hypothesis = canonicalize_verification(
                candidate,
                output,
                incident_id=state["incident_id"],
                affected_services=set(state["affected_services"]),
                evidence=evidence,
            )
            verified.append(hypothesis)
            for request in (*candidate.next_evidence_requests, *hypothesis.next_evidence_requests):
                requests[_request_key(request)] = request
        await self._artifact_store.save_hypotheses(
            UUID(state["run_id"]), state["incident_id"], verified
        )
        completed = set(state.get("completed_request_keys", []))
        pending = [request for key, request in sorted(requests.items()) if key not in completed]
        return {
            "hypotheses": [item.model_dump(mode="json") for item in verified],
            "pending_requests": [item.model_dump(mode="json") for item in pending],
            "usage": self._gateway.usage.model_dump(mode="json"),
        }

    async def _collect_additional_evidence(self, state: InvestigatorState) -> InvestigatorState:
        if self._additional_tools is None:
            return {"pending_requests": []}
        run_id = UUID(state["run_id"])
        usage = await self._artifact_store.usage_for_run(run_id)
        remaining = self._settings.investigator_max_tool_calls - usage.tool_calls
        requests = [
            AdditionalEvidenceRequest.model_validate(item)
            for item in state.get("pending_requests", [])
        ][: max(0, remaining)]
        evidence = {item.id: item for item in _state_evidence(state)}
        window = EvidenceWindow.model_validate(state["window"])
        services = {EvidenceService(item) for item in state["affected_services"]}
        completed = set(state.get("completed_request_keys", []))
        for request in requests:
            await self._additional_tools.execute(
                request,
                run_id=run_id,
                incident_id=state["incident_id"],
                scope_services=services,
                window=window,
                evidence=evidence,
                iteration=state["iteration"],
            )
            completed.add(_request_key(request))
        refreshed = await self._load_current_evidence(state)
        return {
            "evidence": [item.model_dump(mode="json") for item in refreshed],
            "pending_requests": [],
            "completed_request_keys": sorted(completed),
            "usage": (await self._artifact_store.usage_for_run(run_id)).model_dump(mode="json"),
        }

    async def _validate_sufficiency(self, state: InvestigatorState) -> InvestigatorState:
        hypotheses = _state_hypotheses(state)
        eligible = eligible_hypotheses(
            hypotheses,
            confidence_threshold=self._settings.investigator_root_confidence_threshold,
        )
        return {"eligible_hypothesis_ids": [item.id for item in eligible]}

    async def _generate_report(self, state: InvestigatorState) -> InvestigatorState:
        hypotheses = _state_hypotheses(state)
        eligible_ids = set(state.get("eligible_hypothesis_ids", []))
        eligible = [item for item in hypotheses if item.id in eligible_ids]
        synthesis = await self._gateway.call(
            operation=ModelOperation.SYNTHESIZE_REPORT,
            response_model=ReportSynthesis,
            instructions=BASE_INSTRUCTIONS + SYNTHESIZE_REPORT_INSTRUCTIONS,
            payload={
                "incident": _incident_context(state),
                "hypotheses": [item.model_dump(mode="json") for item in hypotheses],
                "eligible_hypothesis_ids": sorted(eligible_ids),
                "evidence_ids": [item.id for item in _state_evidence(state)],
            },
            run_id=state["run_id"],
            incident_id=state["incident_id"],
            logical_key="final",
        )
        report = build_report(
            synthesis,
            run_id=UUID(state["run_id"]),
            incident_id=state["incident_id"],
            title=state["title"],
            affected_services=state["affected_services"],
            severity=self._require_claim().severity,
            hypotheses=hypotheses,
            eligible=eligible,
            evidence=_state_evidence(state),
            timeline=_state_timeline(state).items,
            generated_at=self._clock(),
        )
        await self._artifact_store.save_report(UUID(state["run_id"]), report)
        return {
            "report": report.model_dump(mode="json"),
            "usage": self._gateway.usage.model_dump(mode="json"),
        }

    def _after_verification(self, state: InvestigatorState) -> str:
        pending = state.get("pending_requests", [])
        usage = RunUsage.model_validate(state.get("usage", {}))
        if (
            pending
            and self._additional_tools is not None
            and state.get("iteration", 0) < self._settings.investigator_max_iterations
            and usage.tool_calls < self._settings.investigator_max_tool_calls
        ):
            return "additional"
        return "sufficiency"

    async def _load_current_evidence(self, state: InvestigatorState) -> tuple[EvidenceItem, ...]:
        evidence = await self._evidence_store.all_evidence(state["incident_id"])
        window = EvidenceWindow.model_validate(state["window"])
        scoped = [
            item
            for item in evidence
            if item.window.end >= window.start and item.window.start <= window.end
        ]
        latest: dict[tuple[str, str, str, str, str], EvidenceItem] = {}
        for item in scoped:
            parameters = item.query_parameters
            key = (
                item.source.value,
                item.query_template.value,
                str(parameters.get("service", "")),
                str(parameters.get("trace_id", "")),
                str(parameters.get("timestamp", "")),
            )
            previous = latest.get(key)
            if previous is None or (item.collected_at, item.id) > (
                previous.collected_at,
                previous.id,
            ):
                latest[key] = item
        return tuple(sorted(latest.values(), key=lambda item: (item.observed_at, item.id)))

    def _require_claim(self) -> WorkerClaim:
        if self._claim is None:
            raise RuntimeError("investigator workflow has no active claim")
        return self._claim

    def _observe_workflow(
        self,
        report: IncidentReport,
        state: InvestigatorState,
        duration: float,
        outcome: str,
    ) -> None:
        if self._telemetry is not None:
            self._telemetry.metrics.observe_investigation(
                outcome=outcome,
                duration_seconds=duration,
                iterations=state.get("iteration", 0),
                hypothesis_count=len(report.hypotheses),
                confidence=report.confidence,
            )

    def _observe_failure(self, duration: float) -> None:
        if self._telemetry is not None:
            self._telemetry.metrics.observe_investigation(
                outcome="failed",
                duration_seconds=duration,
                iterations=0,
                hypothesis_count=0,
                confidence=0,
            )


def _initial_state(claim: WorkerClaim) -> InvestigatorState:
    return {
        "run_id": str(claim.run_id),
        "incident_id": claim.incident_id,
        "title": claim.incident_title,
        "service": claim.service,
        "affected_services": list(claim.affected_services),
        "severity": claim.severity.value,
        "window": {
            "start": claim.investigation_window_start.astimezone(UTC).isoformat(),
            "end": claim.investigation_window_end.astimezone(UTC).isoformat(),
        },
        "iteration": 0,
        "completed_request_keys": [],
        "usage": RunUsage().model_dump(mode="json"),
    }


def _state_evidence(state: InvestigatorState) -> tuple[EvidenceItem, ...]:
    return tuple(EvidenceItem.model_validate(item) for item in state.get("evidence", []))


def _state_hypotheses(state: InvestigatorState) -> tuple[Hypothesis, ...]:
    return tuple(Hypothesis.model_validate(item) for item in state.get("hypotheses", []))


def _state_timeline(state: InvestigatorState) -> EvidenceTimelinePage:
    items = state.get("timeline", [])
    return EvidenceTimelinePage.model_validate(
        {"items": items, "total": len(items), "limit": 100, "offset": 0}
    )


def _incident_context(state: InvestigatorState) -> dict[str, object]:
    return {
        "incident_id": state["incident_id"],
        "title": state["title"],
        "primary_service": state["service"],
        "affected_services": state["affected_services"],
        "severity": state["severity"],
        "window": state["window"],
    }


def _bounded_evidence_context(
    evidence: Sequence[EvidenceItem], max_chars: int
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    used = 2
    newest = sorted(evidence, key=lambda item: (item.collected_at, item.id), reverse=True)
    for item in newest:
        safe = redact_value(
            {
                "id": item.id,
                "source": item.source.value,
                "type": item.type.value,
                "status": item.status.value,
                "observed_at": item.observed_at.isoformat(),
                "window": item.window.model_dump(mode="json"),
                "summary": item.summary,
                "payload": item.payload,
                "query_template": item.query_template.value,
            },
            max_depth=5,
            max_collection_items=20,
        )
        if not isinstance(safe, dict):
            continue
        size = len(json.dumps(safe, sort_keys=True, default=str))
        if records and used + size > max_chars:
            continue
        if used + size > max_chars:
            safe = {
                "id": item.id,
                "source": item.source.value,
                "type": item.type.value,
                "status": item.status.value,
                "summary": item.summary[:512],
            }
            size = len(json.dumps(safe, sort_keys=True))
        if used + size <= max_chars:
            records.append(cast(dict[str, object], safe))
            used += size
    records.reverse()
    return records


def _bounded_timeline_context(timeline: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: item[key]
            for key in ("id", "evidence_id", "timestamp", "source", "summary")
            if key in item
        }
        for item in timeline[-20:]
    ]


def _request_key(request: AdditionalEvidenceRequest) -> str:
    return f"{request.kind.value}:{request.service.value}:{request.anchor_evidence_id}"


__all__ = ["InvestigatorState", "InvestigatorWorkflow"]
