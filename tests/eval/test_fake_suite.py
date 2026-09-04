"""Deterministic fake-provider eval suite for CI (Stage 09).

Runs the real LangGraph workflow offline for every enabled scenario with
fixture evidence and a scripted provider. No network, no credentials, no
cost. Failing scenarios fail the suite with per-scenario diagnostics.
"""

from pathlib import Path

from packages.evals.grading import summarize
from packages.evals.runner import run_fake_dataset_sync

SCENARIOS_DIR = Path("evals/scenarios")


def test_fake_eval_suite_all_scenarios_pass() -> None:
    """Every enabled scenario must grade fully correct offline."""

    result = run_fake_dataset_sync(SCENARIOS_DIR, dataset_version="all")
    assert result.summary.scenario_count >= 12
    failures = [grade for grade in result.grades if not grade.passed]
    assert not failures, f"failing scenarios: {[grade.scenario_id for grade in failures]}"


def test_fake_eval_suite_core_v1_passes() -> None:
    """The seven core scenarios pass as the regression gate."""

    result = run_fake_dataset_sync(SCENARIOS_DIR, dataset_version="v1")
    assert result.summary.scenario_count == 7
    assert result.summary.passed_count == 7
    assert result.summary.root_cause_accuracy == 1.0


def test_fake_eval_suite_extended_has_three_nulls() -> None:
    """The extended dataset contains at least two null-answer cases."""

    result = run_fake_dataset_sync(SCENARIOS_DIR, dataset_version="v1-extended")
    assert result.summary.scenario_count == 12
    nulls = [scenario for scenario in result.scenarios if scenario.expectation.expect_null]
    assert len(nulls) >= 3
    assert {"SCN-007", "SCN-008", "SCN-009"} <= {scenario.scenario_id for scenario in nulls}
    assert result.summary.passed_count == 12


def test_fake_eval_summary_metrics_are_consistent() -> None:
    """Aggregate metrics cover the EVALS contract without invented numbers."""

    result = run_fake_dataset_sync(SCENARIOS_DIR, dataset_version="v1")
    summary = summarize(result.summary.dataset_version, result.grades, result.metadatas)
    assert summary.scenario_count == 7
    assert summary.root_cause_accuracy == 1.0
    assert summary.service_accuracy == 1.0
    assert summary.evidence_grounding_rate == 1.0
    assert summary.recommendation_safety_rate == 1.0
    assert summary.total_estimated_cost_usd == 0.0
