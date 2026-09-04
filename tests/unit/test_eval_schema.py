"""Unit coverage for the versioned eval scenario schema (Stage 09)."""

from pathlib import Path

from packages.evals.scenario import load_enabled_scenarios, load_scenario

SCENARIOS_DIR = Path("evals/scenarios")


def test_all_scenario_files_validate_and_have_unique_ids() -> None:
    """Every JSON file loads without hardcoding IDs (ready to grow to 20+)."""

    scenarios = load_enabled_scenarios(SCENARIOS_DIR)
    assert len(scenarios) >= 7
    ids = [scenario.scenario_id for scenario in scenarios]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for scenario in scenarios:
        assert scenario.schema_version == "1.0"
        assert scenario.scenario_version >= 1
        assert scenario.title
        assert scenario.expectation.affected_service


def test_core_seven_cover_required_faults() -> None:
    """The initial dataset spans all seven EVALS faults plus healthy."""

    scenarios = {item.scenario_id: item for item in load_enabled_scenarios(SCENARIOS_DIR)}
    for required in ("SCN-001", "SCN-002", "SCN-003", "SCN-004", "SCN-005", "SCN-006", "SCN-007"):
        assert required in scenarios
    assert scenarios["SCN-001"].fault.name == "slow_database"
    assert scenarios["SCN-002"].fault.name == "pool_exhaustion"
    assert scenarios["SCN-003"].fault.name == "bad_deployment"
    assert scenarios["SCN-004"].fault.name == "inventory_timeout"
    assert scenarios["SCN-005"].fault.name == "cpu_saturation"
    assert scenarios["SCN-006"].fault.name == "high_error_rate"
    assert scenarios["SCN-007"].fault.name == "healthy"
    assert scenarios["SCN-007"].expectation.expect_null is True


def test_extended_dataset_has_two_null_cases_and_all_edge_types() -> None:
    """Edge fixtures cover the five required types with at least two nulls."""

    scenarios = load_enabled_scenarios(SCENARIOS_DIR)
    edges = {scenario.edge for scenario in scenarios}
    for required in (
        "missing_source",
        "noisy_signal",
        "unrelated_deployment",
        "ambiguous",
        "prompt_injection",
    ):
        assert required in edges
    nulls = [item for item in scenarios if item.expectation.expect_null]
    assert len(nulls) >= 3  # SCN-007 healthy + SCN-008/009 edge nulls
    assert {"SCN-008", "SCN-009"} <= {item.scenario_id for item in nulls}


def test_bad_deployment_expects_approval_gated_rollback() -> None:
    """Only the deployment regression expects a mutating recommendation."""

    scenario = load_scenario(SCENARIOS_DIR / "SCN-003.json")
    assert scenario.expectation.expected_recommendation == "rollback_deployment"
    assert scenario.expectation.requires_approval is True


def test_traffic_bounds_are_safe() -> None:
    """No scenario may request unbounded load."""

    for scenario in load_enabled_scenarios(SCENARIOS_DIR):
        assert 1 <= scenario.traffic.count <= 12
        assert scenario.traffic.poll_deadline_seconds <= 180
        assert scenario.budgets.max_cost_usd <= 100
