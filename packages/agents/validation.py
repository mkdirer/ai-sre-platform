"""Deterministic grounding and sufficiency enforcement for model outputs."""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from packages.models.evidence import (
    CollectionStatus,
    EvidenceItem,
    EvidenceService,
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
    TimelineEvent,
)
from packages.models.incidents import IncidentSeverity
from packages.models.investigation import (
    Hypothesis,
    HypothesisCandidate,
    HypothesisStatus,
    HypothesisVerification,
    IncidentReport,
    Recommendation,
    RecommendationAction,
    RecommendationProposal,
    ReportStatus,
    ReportSynthesis,
    RootCauseCategory,
)

_SERVICE_PATTERN = re.compile(r"\b[A-Za-z0-9._-]+-service\b")
_PUBLIC_ID_PATTERN = re.compile(r"\b(?:INC|EVD|EVT|DEP)-[A-F0-9]{16,24}\b")
_TRACE_PATTERN = re.compile(r"\b[a-f0-9]{32}\b")
_COMMIT_PATTERN = re.compile(r"\b[a-f0-9]{40,64}\b")
_TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b"
)


class GroundingValidationError(ValueError):
    """Model output referenced absent evidence or unsupported incident facts."""


def stable_hypothesis_id(
    incident_id: str,
    category: RootCauseCategory,
    description: str,
) -> str:
    canonical = json.dumps(
        {
            "incident_id": incident_id,
            "category": category.value,
            "description": " ".join(description.casefold().split()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"HYP-{hashlib.sha256(canonical.encode()).hexdigest()[:24].upper()}"


def stable_report_id(run_id: UUID) -> str:
    return f"RPT-{hashlib.sha256(str(run_id).encode()).hexdigest()[:24].upper()}"


def validate_candidates(
    candidates: Sequence[HypothesisCandidate],
    *,
    incident_id: str,
    affected_services: set[str],
    evidence: Sequence[EvidenceItem],
    minimum: int,
) -> None:
    """Reject duplicate, undercomplete, cross-incident, or invented candidate facts."""

    collected_types = {item.type for item in evidence if item.status == CollectionStatus.COLLECTED}
    if len(collected_types) >= 2 and len(evidence) >= minimum and len(candidates) < minimum:
        raise GroundingValidationError("model returned too few competing hypotheses")
    categories = [item.category for item in candidates]
    if len(categories) != len(set(categories)):
        raise GroundingValidationError("competing hypotheses must use distinct categories")
    known = _evidence_map(evidence, incident_id)
    context = _known_context(evidence, incident_id, affected_services)
    for candidate in candidates:
        _require_evidence_ids(candidate.initial_evidence_ids, known)
        _validate_supported_identifiers(candidate.description, affected_services, context)
        for request in candidate.next_evidence_requests:
            _validate_request(
                request.anchor_evidence_id, request.service.value, known, affected_services
            )


def canonicalize_verification(
    candidate: HypothesisCandidate,
    verification: HypothesisVerification,
    *,
    incident_id: str,
    affected_services: set[str],
    evidence: Sequence[EvidenceItem],
) -> Hypothesis:
    """Apply ownership, contradiction, semantic-support, and confidence policy."""

    known = _evidence_map(evidence, incident_id)
    _require_evidence_ids(verification.supporting_evidence_ids, known)
    _require_evidence_ids(verification.contradicting_evidence_ids, known)
    context = _known_context(evidence, incident_id, affected_services)
    _validate_supported_identifiers(verification.reasoning_summary, affected_services, context)
    for request in verification.next_evidence_requests:
        _validate_request(
            request.anchor_evidence_id, request.service.value, known, affected_services
        )

    supporting = [known[item] for item in verification.supporting_evidence_ids]
    supported = bool(supporting) and _category_supported(candidate.category, supporting)
    status = verification.status
    confidence = verification.confidence
    if verification.contradicting_evidence_ids and status == HypothesisStatus.VERIFIED:
        status = HypothesisStatus.REJECTED
    if not supported:
        confidence = min(confidence, 0.30)
        if status == HypothesisStatus.VERIFIED:
            status = HypothesisStatus.INCONCLUSIVE
    if not verification.supporting_evidence_ids:
        confidence = min(confidence, 0.30)
    return Hypothesis(
        id=stable_hypothesis_id(incident_id, candidate.category, candidate.description),
        incident_id=incident_id,
        category=candidate.category,
        description=candidate.description,
        status=status,
        confidence=confidence,
        supporting_evidence_ids=verification.supporting_evidence_ids,
        contradicting_evidence_ids=verification.contradicting_evidence_ids,
        reasoning_summary=verification.reasoning_summary,
        next_evidence_requests=verification.next_evidence_requests,
    )


def eligible_hypotheses(
    hypotheses: Sequence[Hypothesis], *, confidence_threshold: float
) -> tuple[Hypothesis, ...]:
    """Return only mechanically eligible root-cause candidates in stable rank order."""

    eligible = [
        item
        for item in hypotheses
        if item.status == HypothesisStatus.VERIFIED
        and item.confidence >= confidence_threshold
        and item.supporting_evidence_ids
        and not item.contradicting_evidence_ids
    ]
    return tuple(sorted(eligible, key=lambda item: (-item.confidence, item.id)))


def build_report(
    synthesis: ReportSynthesis,
    *,
    run_id: UUID,
    incident_id: str,
    title: str,
    affected_services: Sequence[str],
    severity: IncidentSeverity,
    hypotheses: Sequence[Hypothesis],
    eligible: Sequence[Hypothesis],
    evidence: Sequence[EvidenceItem],
    timeline: Sequence[TimelineEvent],
    generated_at: datetime,
    knowledge_hits: Sequence[object] | None = None,
    knowledge_references: Sequence[str] | None = None,
) -> IncidentReport:
    """Assemble a report whose factual fields are all code-owned or evidence-derived.

    Historical knowledge is supporting context only: it is retained as citations
    but can never substitute for current telemetry evidence when selecting RCA.
    """

    known = _evidence_map(evidence, incident_id)
    eligible_by_id = {item.id: item for item in eligible}
    selected: Hypothesis | None = None
    if synthesis.selected_hypothesis_id is not None:
        selected = eligible_by_id.get(synthesis.selected_hypothesis_id)
        if selected is None:
            raise GroundingValidationError("report selected an ineligible hypothesis")
    elif eligible:
        raise GroundingValidationError("report omitted an eligible root-cause hypothesis")

    service_values = set(affected_services)
    recommendations = _build_recommendations(
        synthesis.recommendations,
        selected=selected,
        incident_id=incident_id,
        run_id=run_id,
        services=service_values,
        evidence=known,
    )
    referenced = set[str]()
    for hypothesis in hypotheses:
        referenced.update(hypothesis.supporting_evidence_ids)
        referenced.update(hypothesis.contradicting_evidence_ids)
    for recommendation in recommendations:
        referenced.update(recommendation.rationale_evidence_ids)
    _require_evidence_ids(referenced, known)

    known_knowledge_ids = _knowledge_id_set(knowledge_hits)
    requested_knowledge = list(knowledge_references or [])
    if knowledge_hits is not None:
        for citation in requested_knowledge:
            if not citation.startswith("KNW-"):
                raise GroundingValidationError("knowledge citations must use KNW- chunk IDs")
        unknown_knowledge = sorted(set(requested_knowledge) - known_knowledge_ids)
        if unknown_knowledge:
            raise GroundingValidationError(
                f"unknown knowledge citations: {','.join(unknown_knowledge)}"
            )
    elif requested_knowledge:
        raise GroundingValidationError("knowledge citations require retrieved context")

    limitations = _data_gaps(evidence)
    if selected is None:
        limitations.append("No verified hypothesis met the configured evidence threshold")
        status = ReportStatus.INSUFFICIENT_EVIDENCE
        root_cause = None
        root_cause_summary = None
        confidence = 0.0
        summary = f"{title}: insufficient evidence for a supported root cause"
        recommendations = tuple(
            item for item in recommendations if item.action_type == RecommendationAction.NO_ACTION
        )
    else:
        root_cause = selected.category
        root_cause_summary = _root_cause_summary(selected.category, affected_services[0])
        confidence = selected.confidence
        summary = f"{title}: {root_cause_summary}"
        status = (
            ReportStatus.WAITING_FOR_APPROVAL
            if any(item.requires_approval for item in recommendations)
            else ReportStatus.COMPLETE
        )

    return IncidentReport(
        id=stable_report_id(run_id),
        incident_id=incident_id,
        title=title,
        affected_services=[EvidenceService(service) for service in affected_services],
        severity=severity,
        summary=summary,
        root_cause=root_cause,
        root_cause_summary=root_cause_summary,
        confidence=confidence,
        timeline=list(timeline[:100]),
        hypotheses=list(hypotheses),
        evidence_references=sorted(referenced),
        knowledge_references=sorted(set(requested_knowledge)),
        recommendations=list(recommendations),
        related_incident_ids=[],
        limitations=limitations,
        status=status,
        generated_at=generated_at.astimezone(UTC),
    )


def _knowledge_id_set(knowledge_hits: Sequence[object] | None) -> set[str]:
    """Extract KNW- chunk IDs from retrieved hits without trusting model output."""

    if not knowledge_hits:
        return set()
    ids: set[str] = set()
    for hit in knowledge_hits:
        chunk_id = getattr(hit, "chunk_id", None)
        if isinstance(chunk_id, str) and chunk_id.startswith("KNW-"):
            ids.add(chunk_id)
    return ids


def _evidence_map(evidence: Sequence[EvidenceItem], incident_id: str) -> dict[str, EvidenceItem]:
    known: dict[str, EvidenceItem] = {}
    for item in evidence:
        if item.incident_id != incident_id:
            raise GroundingValidationError("cross-incident evidence entered investigator context")
        known[item.id] = item
    return known


def _require_evidence_ids(evidence_ids: Iterable[str], known: dict[str, EvidenceItem]) -> None:
    missing = sorted(set(evidence_ids) - known.keys())
    if missing:
        raise GroundingValidationError(
            f"unknown or cross-incident evidence IDs: {','.join(missing)}"
        )


def _validate_request(
    evidence_id: str,
    service: str,
    known: dict[str, EvidenceItem],
    affected_services: set[str],
) -> None:
    _require_evidence_ids([evidence_id], known)
    if service not in affected_services:
        raise GroundingValidationError("additional evidence requested for an out-of-scope service")


def _known_context(
    evidence: Sequence[EvidenceItem], incident_id: str, affected_services: set[str]
) -> str:
    return json.dumps(
        {
            "incident_id": incident_id,
            "services": sorted(affected_services),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_supported_identifiers(
    text: str, affected_services: set[str], known_context: str
) -> None:
    unknown_services = set(_SERVICE_PATTERN.findall(text)) - affected_services
    if unknown_services:
        raise GroundingValidationError(
            f"unsupported services in model output: {','.join(sorted(unknown_services))}"
        )
    for pattern, label in (
        (_PUBLIC_ID_PATTERN, "identifier"),
        (_TRACE_PATTERN, "trace"),
        (_COMMIT_PATTERN, "commit"),
        (_TIMESTAMP_PATTERN, "timestamp"),
    ):
        for value in pattern.findall(text):
            if value not in known_context:
                raise GroundingValidationError(f"unsupported {label} in model output")


def _category_supported(category: RootCauseCategory, evidence: Sequence[EvidenceItem]) -> bool:
    collected = [item for item in evidence if item.status == CollectionStatus.COLLECTED]
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in collected],
        sort_keys=True,
        default=str,
    ).casefold()
    templates = {item.query_template for item in collected}
    types = {item.type for item in collected}
    if category == RootCauseCategory.DATABASE_LATENCY:
        explicit = any(
            marker in serialized
            for marker in ("slow_database", "database latency", "db latency", "database query")
        )
        return explicit or (
            QueryTemplate.METRIC_SERVICE_LATENCY in templates
            and EvidenceType.TRACE in types
            and "duration" in serialized
        )
    if category == RootCauseCategory.DB_POOL_SATURATION:
        return QueryTemplate.METRIC_DB_POOL_USAGE in templates and "pool" in serialized
    elif category == RootCauseCategory.BAD_DEPLOYMENT:
        has_change = _has_distinct_deployment_versions(collected)
        return has_change and any(item.type != EvidenceType.DEPLOYMENT for item in collected)
    elif category == RootCauseCategory.UPSTREAM_TIMEOUT:
        return "timeout" in serialized and bool(types & {EvidenceType.LOG, EvidenceType.TRACE})
    elif category == RootCauseCategory.CPU_SATURATION:
        return QueryTemplate.METRIC_SERVICE_CPU in templates
    elif category == RootCauseCategory.APPLICATION_ERRORS:
        return QueryTemplate.LOG_SERVICE_ERRORS in templates or (
            QueryTemplate.METRIC_SERVICE_ERROR_RATE in templates
            and not any(marker in serialized for marker in ("maximum 0 ratio", '"value": 0'))
        )
    return False  # type: ignore[unreachable]


def _has_distinct_deployment_versions(evidence: Sequence[EvidenceItem]) -> bool:
    versions: set[str] = set()
    for item in evidence:
        if item.type != EvidenceType.DEPLOYMENT:
            continue
        payload = item.payload
        for key in ("current", "previous"):
            deployment = payload.get(key)
            if isinstance(deployment, dict) and isinstance(deployment.get("version"), str):
                versions.add(deployment["version"])
    return len(versions) >= 2


def _build_recommendations(
    proposals: Sequence[RecommendationProposal],
    *,
    selected: Hypothesis | None,
    incident_id: str,
    run_id: UUID,
    services: set[str],
    evidence: dict[str, EvidenceItem],
) -> tuple[Recommendation, ...]:
    recommendations: list[Recommendation] = []
    for index, proposal in enumerate(proposals):
        if proposal.target.value not in services:
            raise GroundingValidationError("recommendation targets an out-of-scope service")
        _require_evidence_ids(proposal.rationale_evidence_ids, evidence)
        parameters: dict[str, object] = {}
        requires_approval = False
        if proposal.action_type == RecommendationAction.INVESTIGATE_DATABASE:
            if selected is None or selected.category not in {
                RootCauseCategory.DATABASE_LATENCY,
                RootCauseCategory.DB_POOL_SATURATION,
            }:
                raise GroundingValidationError("database recommendation lacks a database cause")
            parameters = {"mode": "read_only"}
        elif proposal.action_type == RecommendationAction.ROLLBACK_DEPLOYMENT:
            if selected is None or selected.category != RootCauseCategory.BAD_DEPLOYMENT:
                raise GroundingValidationError("rollback recommendation lacks a deployment cause")
            previous = _previous_deployment(
                evidence[item] for item in proposal.rationale_evidence_ids
            )
            if previous is None:
                raise GroundingValidationError(
                    "rollback recommendation lacks a previous deployment"
                )
            parameters = previous
            requires_approval = True
        stable = hashlib.sha256(
            f"{run_id}:{proposal.action_type.value}:{proposal.target.value}:{index}".encode()
        ).hexdigest()
        recommendations.append(
            Recommendation(
                id=f"REC-{stable[:24].upper()}",
                action_type=proposal.action_type,
                target=proposal.target,
                parameters=parameters,
                rationale_evidence_ids=proposal.rationale_evidence_ids,
                risk=proposal.risk,
                reversible=proposal.reversible,
                requires_approval=requires_approval,
                status="waiting_for_approval" if requires_approval else "proposed",
            )
        )
    return tuple(recommendations)


def _previous_deployment(evidence: Iterable[EvidenceItem]) -> dict[str, object] | None:
    for item in evidence:
        previous = item.payload.get("previous")
        if not isinstance(previous, dict):
            continue
        deployment_id = previous.get("id")
        version = previous.get("version")
        if isinstance(deployment_id, str) and isinstance(version, str):
            return {"deployment_id": deployment_id, "version": version}
    return None


def _data_gaps(evidence: Sequence[EvidenceItem]) -> list[str]:
    gaps: list[str] = []
    for source in EvidenceSource:
        source_items = [item for item in evidence if item.source == source]
        if not any(item.status == CollectionStatus.COLLECTED for item in source_items):
            outcomes = sorted({item.status.value for item in source_items}) or ["not_collected"]
            gaps.append(f"No collected {source.value} evidence ({','.join(outcomes)})")
    return gaps


def _root_cause_summary(category: RootCauseCategory, service: str) -> str:
    summaries = {
        RootCauseCategory.DATABASE_LATENCY: "database latency",
        RootCauseCategory.DB_POOL_SATURATION: "database pool saturation",
        RootCauseCategory.BAD_DEPLOYMENT: "deployment regression",
        RootCauseCategory.UPSTREAM_TIMEOUT: "upstream timeout",
        RootCauseCategory.CPU_SATURATION: "CPU saturation",
        RootCauseCategory.APPLICATION_ERRORS: "application errors",
    }
    return f"{summaries[category]} affecting {service}"


__all__ = [
    "GroundingValidationError",
    "build_report",
    "canonicalize_verification",
    "eligible_hypotheses",
    "stable_hypothesis_id",
    "stable_report_id",
    "validate_candidates",
]
