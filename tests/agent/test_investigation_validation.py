"""Deterministic grounding/sufficiency unit tests for investigator validation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.agents.validation import (
    GroundingValidationError,
    build_report,
    canonicalize_verification,
    eligible_hypotheses,
    stable_hypothesis_id,
    stable_report_id,
    validate_candidates,
)
from packages.models.evidence import (
    EvidenceSource,
    EvidenceType,
    QueryTemplate,
)
from packages.models.incidents import IncidentSeverity
from packages.models.investigation import (
    Hypothesis,
    HypothesisCandidate,
    HypothesisCandidates,
    HypothesisVerification,
    RecommendationAction,
    RecommendationProposal,
    RecommendationRisk,
    ReportStatus,
    ReportSynthesis,
    RootCauseCategory,
)
from tests.agent.helpers import (
    INCIDENT_ID,
    OTHER_INCIDENT_ID,
    RUN_ID,
    TRACE_ID,
    evd_id,
    make_item,
    make_window,
)

METRIC_ID = evd_id(1)
TRACE_ID_EVD = evd_id(3)
DEPLOYMENT_ID = evd_id(4)
DESCRIPTION = "Payment persistence slowed by the injected database delay"


def _evidence() -> list:
    window = make_window()
    return [
        make_item(
            1,
            summary="latency elevation with persistence delay marker",
            payload={"observation": "slow_database delay", "duration_ms": 2400},
            window=window,
        ),
        make_item(
            3,
            source=EvidenceSource.TEMPO,
            type=EvidenceType.TRACE,
            template=QueryTemplate.TRACE_SLOW_SERVICE,
            summary="slow trace",
            payload={"trace_id": TRACE_ID, "duration_ms": 2510},
            window=window,
        ),
        make_item(
            4,
            source=EvidenceSource.DEPLOYMENT_STORE,
            type=EvidenceType.DEPLOYMENT,
            template=QueryTemplate.DEPLOYMENT_CURRENT_PREVIOUS,
            summary="deployment unchanged",
            payload={
                "current": {"id": "DEP-0002", "version": "0.1.0"},
                "previous": {"id": "DEP-0001", "version": "0.1.0"},
            },
            window=window,
        ),
    ]


def _candidate(**overrides) -> HypothesisCandidate:
    values = {
        "category": RootCauseCategory.DATABASE_LATENCY,
        "description": DESCRIPTION,
        "initial_evidence_ids": [METRIC_ID, TRACE_ID_EVD],
    }
    values.update(overrides)
    return HypothesisCandidate(**values)


def _verification(**overrides) -> HypothesisVerification:
    values = {
        "status": "verified",
        "confidence": 0.8,
        "supporting_evidence_ids": [METRIC_ID, TRACE_ID_EVD],
        "contradicting_evidence_ids": [],
        "reasoning_summary": "Latency and spans coincide with the delay notice",
    }
    values.update(overrides)
    return HypothesisVerification(**values)


def test_stable_ids_are_deterministic() -> None:
    """Identical inputs always produce identical hypothesis and report IDs."""

    first = stable_hypothesis_id(INCIDENT_ID, RootCauseCategory.DATABASE_LATENCY, DESCRIPTION)
    again = stable_hypothesis_id(
        INCIDENT_ID,
        RootCauseCategory.DATABASE_LATENCY,
        "  payment  PERSISTENCE slowed BY the injected database delay  ",
    )
    other = stable_hypothesis_id(OTHER_INCIDENT_ID, RootCauseCategory.DATABASE_LATENCY, DESCRIPTION)

    assert first == again
    assert first != other
    assert stable_report_id(RUN_ID) == stable_report_id(RUN_ID)
    assert stable_report_id(RUN_ID) != stable_report_id(UUID(int=0))


def test_candidates_reject_unknown_evidence_id() -> None:
    """References to absent evidence fail closed."""

    with pytest.raises(GroundingValidationError, match="unknown or cross-incident"):
        validate_candidates(
            [_candidate(initial_evidence_ids=[evd_id(99)])],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=_evidence(),
            minimum=1,
        )


def test_candidates_reject_cross_incident_evidence() -> None:
    """Well-formed IDs from another incident are still rejected."""

    foreign = make_item(7, incident_id=OTHER_INCIDENT_ID)
    with pytest.raises(GroundingValidationError, match="cross-incident"):
        validate_candidates(
            [_candidate(initial_evidence_ids=[foreign.id])],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=[*_evidence(), foreign],
            minimum=1,
        )


def test_candidates_reject_duplicate_categories() -> None:
    """Competing hypotheses must explore distinct causal categories."""

    with pytest.raises(GroundingValidationError, match="distinct categories"):
        validate_candidates(
            [_candidate(), _candidate()],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=_evidence(),
            minimum=1,
        )


def test_candidates_require_minimum_when_evidence_is_rich() -> None:
    """A rich evidence set demands genuine competition, not a single story."""

    with pytest.raises(GroundingValidationError, match="too few competing"):
        validate_candidates(
            [_candidate()],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=_evidence(),
            minimum=3,
        )


def test_candidates_reject_invented_identifiers() -> None:
    """Services, traces, commits, and timestamps must come from collected context."""

    invented_trace = "c" * 32
    assert invented_trace not in str([item.model_dump() for item in _evidence()])
    with pytest.raises(GroundingValidationError, match="unsupported trace"):
        validate_candidates(
            [
                _candidate(
                    description="Payment latency confirmed by trace " + invented_trace,
                )
            ],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=_evidence(),
            minimum=1,
        )


def test_candidates_reject_out_of_scope_service() -> None:
    with pytest.raises(GroundingValidationError, match="unsupported services"):
        validate_candidates(
            [_candidate(description="Latency in billing-service confirms the cause")],
            incident_id=INCIDENT_ID,
            affected_services={"payment-service"},
            evidence=_evidence(),
            minimum=1,
        )


def test_verification_requires_disjoint_evidence_lists() -> None:
    """Schema validation rejects self-contradicting model output immediately."""

    with pytest.raises(ValueError, match="disjoint"):
        _verification(
            supporting_evidence_ids=[METRIC_ID],
            contradicting_evidence_ids=[METRIC_ID],
        )


def test_contradiction_flips_verified_to_rejected() -> None:
    """Strong contradiction overrules a model-claimed verification."""

    hypothesis = canonicalize_verification(
        _candidate(),
        _verification(confidence=0.9, contradicting_evidence_ids=[DEPLOYMENT_ID]),
        incident_id=INCIDENT_ID,
        affected_services={"payment-service"},
        evidence=_evidence(),
    )

    assert hypothesis.status == "rejected"
    assert hypothesis.confidence == 0.9
    assert hypothesis.contradicting_evidence_ids == [DEPLOYMENT_ID]


def test_unsupported_hypothesis_gets_low_confidence() -> None:
    """A verified claim without semantic evidence support is capped and downgraded."""

    hypothesis = canonicalize_verification(
        _candidate(
            category=RootCauseCategory.CPU_SATURATION,
            description="CPU saturation drives payment latency",
        ),
        _verification(confidence=0.9),
        incident_id=INCIDENT_ID,
        affected_services={"payment-service"},
        evidence=_evidence(),
    )

    assert hypothesis.status == "inconclusive"
    assert hypothesis.confidence == 0.3


def test_eligibility_requires_verified_supported_uncontradicted() -> None:
    """Only mechanically eligible hypotheses can become the reported root cause."""

    def hypothesis(**overrides) -> Hypothesis:
        values = {
            "id": stable_hypothesis_id(
                INCIDENT_ID, RootCauseCategory.DATABASE_LATENCY, DESCRIPTION
            ),
            "incident_id": INCIDENT_ID,
            "category": RootCauseCategory.DATABASE_LATENCY,
            "description": DESCRIPTION,
            "status": "verified",
            "confidence": 0.8,
            "supporting_evidence_ids": [METRIC_ID],
            "contradicting_evidence_ids": [],
            "reasoning_summary": "grounded",
        }
        values.update(overrides)
        return Hypothesis(**values)

    eligible = eligible_hypotheses(
        [
            hypothesis(),
            hypothesis(
                id=evd_id(5).replace("EVD", "HYP"),
                confidence=0.5,
            ),
            hypothesis(
                id=evd_id(6).replace("EVD", "HYP"),
                contradicting_evidence_ids=[TRACE_ID_EVD],
            ),
            hypothesis(
                id=evd_id(7).replace("EVD", "HYP"),
                status="rejected",
            ),
        ],
        confidence_threshold=0.65,
    )

    assert [item.confidence for item in eligible] == [0.8]


def _report_kwargs(**overrides) -> dict:
    values = {
        "run_id": RUN_ID,
        "incident_id": INCIDENT_ID,
        "title": "Payment latency",
        "affected_services": ["payment-service"],
        "severity": IncidentSeverity.WARNING,
        "evidence": _evidence(),
        "timeline": [],
        "generated_at": datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
    }
    values.update(overrides)
    return values


def _verified_db_hypothesis() -> Hypothesis:
    return canonicalize_verification(
        _candidate(),
        _verification(),
        incident_id=INCIDENT_ID,
        affected_services={"payment-service"},
        evidence=_evidence(),
    )


def test_build_report_selects_only_eligible_hypothesis() -> None:
    """Selecting an ineligible hypothesis, or omitting an eligible one, fails."""

    hypothesis = _verified_db_hypothesis()
    other_id = stable_hypothesis_id(INCIDENT_ID, RootCauseCategory.BAD_DEPLOYMENT, "other story")
    synthesis = ReportSynthesis(
        selected_hypothesis_id=other_id,
        recommendations=[],
    )
    with pytest.raises(GroundingValidationError, match="ineligible"):
        build_report(synthesis, hypotheses=[hypothesis], eligible=[hypothesis], **_report_kwargs())

    with pytest.raises(GroundingValidationError, match="omitted"):
        build_report(
            ReportSynthesis(selected_hypothesis_id=None, recommendations=[]),
            hypotheses=[hypothesis],
            eligible=[hypothesis],
            **_report_kwargs(),
        )


def test_build_report_insufficient_evidence_has_null_cause() -> None:
    """No eligible hypothesis means a null root cause with explicit gaps."""

    report = build_report(
        ReportSynthesis(selected_hypothesis_id=None, recommendations=[]),
        hypotheses=[],
        eligible=[],
        **_report_kwargs(),
    )

    assert report.status == ReportStatus.INSUFFICIENT_EVIDENCE
    assert report.root_cause is None
    assert report.confidence == 0.0
    assert any("No verified hypothesis" in gap for gap in report.limitations)


def test_build_report_rejects_mismatched_rollback() -> None:
    """A rollback proposal requires a deployment cause and a previous version."""

    hypothesis = _verified_db_hypothesis()
    synthesis = ReportSynthesis(
        selected_hypothesis_id=hypothesis.id,
        recommendations=[
            RecommendationProposal(
                action_type=RecommendationAction.ROLLBACK_DEPLOYMENT,
                target="payment-service",
                rationale_evidence_ids=[DEPLOYMENT_ID],
                risk=RecommendationRisk.MEDIUM,
                reversible=True,
            )
        ],
    )
    with pytest.raises(GroundingValidationError, match="deployment cause"):
        build_report(synthesis, hypotheses=[hypothesis], eligible=[hypothesis], **_report_kwargs())


def test_build_report_rejects_out_of_scope_recommendation_target() -> None:
    """Recommendations cannot target services outside the incident scope."""

    hypothesis = _verified_db_hypothesis()
    synthesis = ReportSynthesis(
        selected_hypothesis_id=hypothesis.id,
        recommendations=[
            RecommendationProposal(
                action_type=RecommendationAction.INVESTIGATE_DATABASE,
                target="inventory-service",
                rationale_evidence_ids=[METRIC_ID],
                risk=RecommendationRisk.LOW,
                reversible=True,
            )
        ],
    )
    with pytest.raises(GroundingValidationError, match="out-of-scope"):
        build_report(synthesis, hypotheses=[hypothesis], eligible=[hypothesis], **_report_kwargs())


def test_build_report_keeps_only_no_action_without_cause() -> None:
    """Insufficient-evidence reports never carry mutating proposals."""

    report = build_report(
        ReportSynthesis(
            selected_hypothesis_id=None,
            recommendations=[
                RecommendationProposal(
                    action_type=RecommendationAction.NO_ACTION,
                    target="payment-service",
                    rationale_evidence_ids=[METRIC_ID],
                    risk=RecommendationRisk.LOW,
                    reversible=True,
                ),
            ],
        ),
        hypotheses=[],
        eligible=[],
        **_report_kwargs(),
    )

    assert report.status == ReportStatus.INSUFFICIENT_EVIDENCE
    assert [item.action_type for item in report.recommendations] == [RecommendationAction.NO_ACTION]


def test_build_report_rejects_actionable_proposal_without_cause() -> None:
    """An actionable proposal without a selected cause fails instead of filtering."""

    with pytest.raises(GroundingValidationError, match="lacks a database cause"):
        build_report(
            ReportSynthesis(
                selected_hypothesis_id=None,
                recommendations=[
                    RecommendationProposal(
                        action_type=RecommendationAction.INVESTIGATE_DATABASE,
                        target="payment-service",
                        rationale_evidence_ids=[METRIC_ID],
                        risk=RecommendationRisk.LOW,
                        reversible=True,
                    ),
                ],
            ),
            hypotheses=[],
            eligible=[],
            **_report_kwargs(),
        )


def test_data_gaps_name_missing_sources() -> None:
    """Sources without collected evidence are listed as gaps, not silent."""

    partial = [item for item in _evidence() if item.source == EvidenceSource.PROMETHEUS]
    report = build_report(
        ReportSynthesis(selected_hypothesis_id=None, recommendations=[]),
        hypotheses=[],
        eligible=[],
        **_report_kwargs(evidence=partial),
    )

    assert any("loki" in gap for gap in report.limitations)
    assert any("tempo" in gap for gap in report.limitations)
    assert any("deployment_store" in gap for gap in report.limitations)


def test_candidates_model_rejects_malformed_envelopes() -> None:
    """Structured-output schemas reject empty or oversized hypothesis lists."""

    with pytest.raises(ValueError):
        HypothesisCandidates(hypotheses=[])
    with pytest.raises(ValueError):
        HypothesisCandidates(hypotheses=[_candidate() for _ in range(6)])
