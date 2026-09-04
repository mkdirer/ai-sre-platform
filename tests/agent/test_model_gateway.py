"""Budget, retry, routing, and configuration tests for the model gateway."""

import pytest

from packages.agents.provider import (
    BudgetedModelGateway,
    InvestigatorBudgetExceeded,
    OpenAIResponsesProvider,
    ProviderConfigurationError,
    ProviderResponseError,
)
from packages.models.investigation import (
    HypothesisCandidate,
    HypothesisCandidates,
    ModelOperation,
    ReportSynthesis,
    RootCauseCategory,
    RunUsage,
)
from tests.agent.helpers import (
    INCIDENT_ID,
    RUN_ID,
    InMemoryArtifactStore,
    ScriptedProvider,
    evd_id,
    make_settings,
)


def _empty_candidates() -> HypothesisCandidates:
    return HypothesisCandidates(
        hypotheses=[
            HypothesisCandidate(
                category=RootCauseCategory.DATABASE_LATENCY,
                description="fixture candidate",
                initial_evidence_ids=[evd_id(1)],
            )
        ]
    )


def _gateway(
    provider: ScriptedProvider, artifacts: InMemoryArtifactStore, **settings_overrides
) -> BudgetedModelGateway:
    return BudgetedModelGateway(
        provider=provider,
        store=artifacts,
        settings=make_settings(**settings_overrides),
        usage=RunUsage(),
    )


def _call_kwargs(**overrides) -> dict:
    values = {
        "operation": ModelOperation.GENERATE_HYPOTHESES,
        "response_model": HypothesisCandidates,
        "instructions": "test instructions",
        "payload": {"incident": INCIDENT_ID},
        "run_id": str(RUN_ID),
        "incident_id": INCIDENT_ID,
        "logical_key": "iteration:1",
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_gateway_retries_once_then_returns_output() -> None:
    """A transient provider failure is retried within the bounded attempt budget."""

    provider = ScriptedProvider(
        {
            "HypothesisCandidates": [
                RuntimeError("transient timeout"),
                _empty_candidates(),
            ]
        }
    )
    artifacts = InMemoryArtifactStore()

    output = await _gateway(provider, artifacts).call(**_call_kwargs())

    assert output == _empty_candidates()
    assert len(provider.calls) == 2
    assert [call.status for call in artifacts.calls] == ["failed", "succeeded"]
    assert artifacts.calls[-1].attempt == 2


@pytest.mark.asyncio
async def test_gateway_raises_after_bounded_attempts() -> None:
    """Persistent provider failure surfaces after exactly the configured attempts."""

    provider = ScriptedProvider(
        {"HypothesisCandidates": [RuntimeError("provider down")]},
    )
    artifacts = InMemoryArtifactStore()

    with pytest.raises(ProviderResponseError, match="bounded provider attempts"):
        await _gateway(provider, artifacts).call(**_call_kwargs())

    assert len(provider.calls) == 2
    assert all(call.status == "failed" for call in artifacts.calls)


@pytest.mark.asyncio
async def test_gateway_enforces_context_budget_without_calling_provider() -> None:
    """Oversized payloads are rejected before any provider interaction or spend."""

    provider = ScriptedProvider({"HypothesisCandidates": [_empty_candidates()]})
    artifacts = InMemoryArtifactStore()

    with pytest.raises(InvestigatorBudgetExceeded, match="context character"):
        await _gateway(provider, artifacts).call(**_call_kwargs(payload={"blob": "x" * 40_000}))

    assert provider.calls == []
    assert artifacts.calls == []


@pytest.mark.asyncio
async def test_gateway_enforces_model_call_budget() -> None:
    """The per-run model call cap stops further spend."""

    provider = ScriptedProvider({"HypothesisCandidates": [_empty_candidates()]})
    artifacts = InMemoryArtifactStore()
    gateway = BudgetedModelGateway(
        provider=provider,
        store=artifacts,
        settings=make_settings(investigator_max_model_calls=1),
        usage=RunUsage(model_calls=1),
    )

    with pytest.raises(InvestigatorBudgetExceeded, match="model call budget"):
        await gateway.call(**_call_kwargs())

    assert provider.calls == []


@pytest.mark.asyncio
async def test_gateway_enforces_token_and_cost_budgets() -> None:
    """Projected token overflow and configured cost overflow both fail closed."""

    provider = ScriptedProvider({"HypothesisCandidates": [_empty_candidates()]})
    token_gateway = BudgetedModelGateway(
        provider=provider,
        store=InMemoryArtifactStore(),
        settings=make_settings(investigator_max_total_tokens=1_000),
        usage=RunUsage(input_tokens=900, output_tokens=90),
    )
    with pytest.raises(InvestigatorBudgetExceeded, match="token budget"):
        await token_gateway.call(**_call_kwargs())

    cost_gateway = BudgetedModelGateway(
        provider=provider,
        store=InMemoryArtifactStore(),
        settings=make_settings(
            investigator_max_estimated_cost_usd=0.0001,
            investigator_input_cost_per_million_usd=100.0,
        ),
        usage=RunUsage(),
    )
    with pytest.raises(InvestigatorBudgetExceeded, match="cost budget"):
        await cost_gateway.call(**_call_kwargs())

    assert provider.calls == []


@pytest.mark.asyncio
async def test_gateway_routes_operations_to_configured_models() -> None:
    """Planning and reasoning operations use distinct configured model names."""

    provider = ScriptedProvider(
        {
            "HypothesisCandidates": [_empty_candidates()],
            "ReportSynthesis": [ReportSynthesis(selected_hypothesis_id=None, recommendations=[])],
        }
    )
    artifacts = InMemoryArtifactStore()
    gateway = _gateway(
        provider,
        artifacts,
        investigator_planning_model="plan-model",
        investigator_reasoning_model="reason-model",
    )

    await gateway.call(**_call_kwargs())
    await gateway.call(
        **_call_kwargs(
            operation=ModelOperation.SYNTHESIZE_REPORT,
            response_model=ReportSynthesis,
        )
    )

    assert [call.model for call in artifacts.calls] == ["plan-model", "reason-model"]


@pytest.mark.asyncio
async def test_gateway_records_usage_and_call_metadata() -> None:
    """Successful calls persist token usage for resume-time budget restoration."""

    provider = ScriptedProvider({"HypothesisCandidates": [_empty_candidates()]})
    artifacts = InMemoryArtifactStore()
    gateway = _gateway(provider, artifacts)

    await gateway.call(**_call_kwargs())

    assert gateway.usage.model_calls == 1
    assert gateway.usage.input_tokens == 10
    assert gateway.usage.output_tokens == 10
    (record,) = artifacts.calls
    assert record.operation == ModelOperation.GENERATE_HYPOTHESES.value
    assert record.provider == "fake"
    assert record.metadata == {"response_id": "fake-response"}


def test_openai_provider_requires_an_api_key() -> None:
    """Provider construction fails closed without credentials and touches no network."""

    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        OpenAIResponsesProvider(make_settings())


def test_openai_provider_accepts_configured_key_without_network() -> None:
    """A configured key builds the provider; no request is issued by construction."""

    from pydantic import SecretStr

    provider = OpenAIResponsesProvider(
        make_settings(openai_api_key=SecretStr("test-key-not-a-secret"))
    )

    assert provider.name == "openai"
