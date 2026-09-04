"""Unit coverage for deterministic eval grading (Stage 09).

Every EVALS grading rule gets a targeted case: normalized labels, invented
evidence, wrong mechanisms, coincidental deployments, correct nulls,
unsafe recommendations, and budget enforcement.
"""

from datetime import UTC, datetime
from pathlib import Path

from packages.evals.grading import RunMetadata, grade_report
from packages.evals.scenario import load_scenario
from packages.models.investigation import IncidentReport, RecommendationAction

SCENARIOS_DIR = Path("evals/scenarios")
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _report_for(scenario_id: str) -> tuple[IncidentReport, set[str], set[str], set[str]]:
    """Run the fake fixture workflow and return report plus collection detail."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / f"{scenario_id}.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    known = {item.id for item in evidence}
    templates = {item.query_template.value for item in evidence if str(item.status) == "collected"}
    sources = {item.source.value for item in evidence if str(item.status) == "collected"}
    return report, known, templates, sources


def test_correct_report_passes() -> None:
    """A supported slow-database report grades fully correct."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-001.json")
    report, metadata, evidence = asyncio.run(run_fake_scenario(scenario))
    grade = grade_report(
        scenario,
        report,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={
            item.query_template.value for item in evidence if str(item.status) == "collected"
        },
        collected_sources={
            item.source.value for item in evidence if str(item.status) == "collected"
        },
        metadata=metadata,
    )
    assert grade.passed is True
    assert grade.root_cause_correct and grade.service_correct
    assert grade.evidence_grounded and grade.evidence_sufficient
    assert grade.recommendation_correct and grade.recommendation_safe


def test_wrong_mechanism_with_right_service_is_not_full_match() -> None:
    """Correct service plus contradicted cause fails root-cause grading."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-001.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    mutated = report.model_copy(
        update={"root_cause": "bad_deployment", "root_cause_summary": "deployment regression"}
    )
    grade = grade_report(
        scenario,
        mutated,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={"metric.service_latency_p95"},
        collected_sources={"prometheus"},
        metadata=RunMetadata(),
    )
    assert grade.root_cause_correct is False
    assert grade.service_correct is True
    assert grade.passed is False


def test_invented_evidence_fails_grounding_despite_correct_cause() -> None:
    """A semantically correct cause with an unknown evidence ID still fails."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-001.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    mutated_hypotheses = [
        {**hypothesis.model_dump(), "supporting_evidence_ids": ["EVD-FFFFFFFFFFFFFFFFFFFFFFFF"]}
        if index == 0
        else hypothesis
        for index, hypothesis in enumerate(report.hypotheses)
    ]
    # Rebuild via model validation to keep types strict.
    from packages.models.investigation import Hypothesis

    rebuilt = [
        item if not isinstance(item, dict) else Hypothesis.model_validate(item)
        for item in mutated_hypotheses
    ]
    mutated = report.model_copy(update={"hypotheses": rebuilt})
    grade = grade_report(
        scenario,
        mutated,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={"metric.service_latency_p95"},
        collected_sources={"prometheus"},
        metadata=RunMetadata(),
    )
    assert grade.evidence_grounded is False
    assert grade.unsupported_claims >= 1
    assert grade.passed is False


def test_coincidental_deployment_without_corroboration_fails() -> None:
    """Selecting bad_deployment for the unrelated-deploy fixture is wrong."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-011.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    assert report.root_cause is not None and report.root_cause.value == "database_latency"
    mutated = report.model_copy(
        update={"root_cause": "bad_deployment", "root_cause_summary": "deployment regression"}
    )
    grade = grade_report(
        scenario,
        mutated,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={"metric.service_latency_p95"},
        collected_sources={"prometheus"},
        metadata=RunMetadata(),
    )
    assert grade.root_cause_correct is False
    assert grade.passed is False


def test_correct_null_is_success_and_wrong_null_fails() -> None:
    """Healthy null passes; a guessed cause on healthy data fails."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-007.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    assert report.root_cause is None
    passing = grade_report(
        scenario,
        report,
        known_evidence_ids={item.id for item in evidence},
        collected_templates=set(),
        collected_sources=set(),
        metadata=RunMetadata(),
    )
    assert passing.null_correct is True
    assert passing.passed is True

    guessed = report.model_copy(
        update={
            "root_cause": "database_latency",
            "root_cause_summary": "database latency affecting payment-service",
            "confidence": 0.8,
            "status": "complete",
        }
    )
    failing = grade_report(
        scenario,
        guessed,
        known_evidence_ids={item.id for item in evidence},
        collected_templates=set(),
        collected_sources=set(),
        metadata=RunMetadata(),
    )
    assert failing.null_correct is False
    assert failing.passed is False


def test_non_null_scenario_returning_null_fails() -> None:
    """A null answer on a faulty scenario is scored as a miss."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-002.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    assert report.root_cause is not None
    nulled = report.model_copy(
        update={
            "root_cause": None,
            "root_cause_summary": None,
            "confidence": 0.0,
            "status": "insufficient_evidence",
            "recommendations": [
                item
                for item in report.recommendations
                if item.action_type == RecommendationAction.NO_ACTION
            ]
            or report.recommendations,
        }
    )
    grade = grade_report(
        scenario,
        nulled,
        known_evidence_ids={item.id for item in evidence},
        collected_templates=set(),
        collected_sources=set(),
        metadata=RunMetadata(),
    )
    assert grade.null_correct is False
    assert grade.passed is False


def test_unsafe_rollback_without_expectation_fails_safety() -> None:
    """An unexpected rollback recommendation is an automatic safety failure."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario
    from packages.models.investigation import Recommendation

    scenario = load_scenario(SCENARIOS_DIR / "SCN-001.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    injected = Recommendation.model_validate(
        {
            **report.recommendations[0].model_dump(mode="json"),
            "action_type": "rollback_deployment",
            "requires_approval": True,
            "status": "waiting_for_approval",
        }
    )
    mutated = report.model_copy(update={"recommendations": [injected]})
    grade = grade_report(
        scenario,
        mutated,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={"metric.service_latency_p95"},
        collected_sources={"prometheus"},
        metadata=RunMetadata(),
    )
    assert grade.recommendation_safe is False
    assert grade.passed is False


def test_budget_overrun_fails() -> None:
    """Exceeding tool, cost, or duration budgets fails the scenario."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-001.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    grade = grade_report(
        scenario,
        report,
        known_evidence_ids={item.id for item in evidence},
        collected_templates={"metric.service_latency_p95"},
        collected_sources={"prometheus"},
        metadata=RunMetadata(tool_calls=99, estimated_cost_usd=99.0, duration_seconds=999.0),
    )
    assert grade.budgets_ok is False
    assert grade.passed is False


def test_missing_required_evidence_fails_sufficiency() -> None:
    """Omitting a required template or source is an explicit sufficiency miss."""

    import asyncio

    from packages.evals.fixtures import run_fake_scenario

    scenario = load_scenario(SCENARIOS_DIR / "SCN-002.json")
    report, _, evidence = asyncio.run(run_fake_scenario(scenario))
    grade = grade_report(
        scenario,
        report,
        known_evidence_ids={item.id for item in evidence},
        collected_templates=set(),
        collected_sources=set(),
        metadata=RunMetadata(),
    )
    assert grade.evidence_sufficient is False
    assert grade.passed is False
