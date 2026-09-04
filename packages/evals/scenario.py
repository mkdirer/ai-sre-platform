"""Versioned machine-readable eval scenario schema (Stage 09).

Implements docs/EVALS.md scenario declarations. Files live under
evals/scenarios/*.json with schema_version 1.0 so the set can grow to 20+
without code changes: the loader discovers every file.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ScenarioId = Annotated[str, StringConstraints(pattern=r"^SCN-[0-9]{3}$")]
DatasetVersion = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,32}$")]

SCHEMA_VERSION = "1.0"


class ScenarioFault(BaseModel):
    """Fault activation for one scenario (live runner activates, fake uses fixtures)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    service: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    control_path: Annotated[str, StringConstraints(max_length=128)] = ""
    expect_alert: bool = True
    alertname: Annotated[str, StringConstraints(max_length=128)] = ""


class ScenarioDeployment(BaseModel):
    """Seeded deployment registered before fault activation (live runner)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    environment: Annotated[str, StringConstraints(min_length=1, max_length=32)] = "development"
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    deployed_at_offset_minutes: Annotated[int, Field(ge=-10_000, le=0)] = -10
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] = "0" * 40
    role: Literal["previous_baseline", "current_baseline", "unrelated", "regressed"] = (
        "current_baseline"
    )


class ScenarioExpectation(BaseModel):
    """Graded expectations for root cause, service, evidence, and recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    affected_service: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    accepted_root_causes: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = (
        Field(default_factory=list)
    )
    contradicted_causes: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = (
        Field(default_factory=list)
    )
    expect_null: bool = False
    expected_recommendation: Annotated[str, StringConstraints(min_length=1, max_length=64)] = (
        "no_action"
    )
    requires_approval: bool = False
    required_templates: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = (
        Field(default_factory=list)
    )
    required_sources: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=list
    )


class ScenarioBudgets(BaseModel):
    """Per-scenario run budgets checked by the grader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tool_calls: Annotated[int, Field(ge=1, le=50)] = 16
    max_iterations: Annotated[int, Field(ge=1, le=5)] = 2
    max_duration_seconds: Annotated[float, Field(gt=0, le=600)] = 120.0
    max_cost_usd: Annotated[float, Field(ge=0, le=100)] = 2.0
    max_total_tokens: Annotated[int, Field(ge=100, le=1_000_000)] = 50_000


class ScenarioTraffic(BaseModel):
    """Bounded live traffic (ignored by the offline fake suite)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: Annotated[int, Field(ge=1, le=12)] = 6
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0
    poll_deadline_seconds: Annotated[float, Field(gt=0, le=180)] = 60.0


class EvalScenario(BaseModel):
    """One versioned reproducible eval case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    scenario_id: ScenarioId
    scenario_version: Annotated[int, Field(ge=1)] = 1
    dataset_version: DatasetVersion = "v1"
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=2_000)] = ""
    fault: ScenarioFault
    deployments: list[ScenarioDeployment] = Field(default_factory=list)
    expectation: ScenarioExpectation
    budgets: ScenarioBudgets = Field(default_factory=ScenarioBudgets)
    traffic: ScenarioTraffic = Field(default_factory=ScenarioTraffic)
    edge: Literal[
        "none",
        "missing_source",
        "noisy_signal",
        "unrelated_deployment",
        "ambiguous",
        "prompt_injection",
    ] = "none"
    enabled: bool = True


class EvalDataset(BaseModel):
    """Manifest tying scenario IDs to one dataset version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: DatasetVersion
    schema_version: Literal["1.0"] = "1.0"
    scenarios: list[ScenarioId]


def load_scenario(path: Path) -> EvalScenario:
    """Load and validate one scenario file."""

    return EvalScenario.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_scenarios(directory: Path) -> list[EvalScenario]:
    """Discover every *.json scenario without hardcoding IDs (grows to 20+)."""

    scenarios = [load_scenario(path) for path in sorted(directory.glob("*.json"))]
    seen = [scenario.scenario_id for scenario in scenarios]
    if len(seen) != len(set(seen)):
        raise ValueError(f"duplicate scenario IDs in {directory}")
    return scenarios


def load_enabled_scenarios(directory: Path) -> list[EvalScenario]:
    """Return enabled scenarios in stable ID order."""

    return sorted(
        (scenario for scenario in load_scenarios(directory) if scenario.enabled),
        key=lambda scenario: scenario.scenario_id,
    )
