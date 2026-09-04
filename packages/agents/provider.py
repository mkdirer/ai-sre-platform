"""Structured model provider interface and OpenAI Responses API implementation."""

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from packages.config import Settings
from packages.models.investigation import ModelCallRecord, ModelOperation, RunUsage
from packages.telemetry import TelemetryRuntime, redact_text

OutputT = TypeVar("OutputT", bound=BaseModel)


class ProviderConfigurationError(RuntimeError):
    """The selected provider cannot be used with the configured credentials."""


class ProviderResponseError(RuntimeError):
    """A provider response did not contain schema-validated structured output."""


class InvestigatorBudgetExceeded(RuntimeError):
    """A deterministic run budget prevented another provider or tool call."""


@dataclass(frozen=True)
class ProviderResult[OutputT: BaseModel]:
    """Schema-valid provider response plus optional usage metadata."""

    output: OutputT
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class StructuredModelProvider(Protocol):
    """Replaceable structured-output boundary used by production and deterministic fakes."""

    @property
    def name(self) -> str: ...

    async def complete(
        self,
        *,
        model: str,
        instructions: str,
        input_json: str,
        response_model: type[OutputT],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult[OutputT]: ...

    async def close(self) -> None: ...


class ModelCallStore(Protocol):
    """Persistence operations required by the budgeted model gateway."""

    async def record_call(self, record: ModelCallRecord) -> None: ...


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter using SDK-native Pydantic Structured Outputs."""

    def __init__(self, settings: Settings) -> None:
        api_key = settings.openai_api_key.get_secret_value()
        if not api_key:
            raise ProviderConfigurationError("OPENAI_API_KEY is required when AI is enabled")
        self._client = AsyncOpenAI(api_key=api_key)

    @property
    def name(self) -> str:
        return "openai"

    async def complete(
        self,
        *,
        model: str,
        instructions: str,
        input_json: str,
        response_model: type[OutputT],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult[OutputT]:
        response = await self._client.responses.parse(
            model=model,
            instructions=instructions,
            input=input_json,
            text_format=response_model,
            max_output_tokens=max_output_tokens,
            store=False,
            timeout=timeout_seconds,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ProviderResponseError("provider returned no parsed structured output")
        usage = response.usage
        return ProviderResult(
            output=parsed,
            response_id=response.id,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
        )

    async def close(self) -> None:
        await self._client.close()


class ModelRouter:
    """Configuration-only model selection behind a stable operation interface."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def model_for(self, operation: ModelOperation) -> str:
        if operation == ModelOperation.GENERATE_HYPOTHESES:
            return self._settings.investigator_planning_model
        return self._settings.investigator_reasoning_model


class BudgetedModelGateway:
    """Enforce retry/call/context/token/cost bounds and persist every provider attempt."""

    def __init__(
        self,
        *,
        provider: StructuredModelProvider,
        store: ModelCallStore,
        settings: Settings,
        usage: RunUsage,
        telemetry: TelemetryRuntime | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._settings = settings
        self._router = ModelRouter(settings)
        self._usage = usage
        self._telemetry = telemetry

    @property
    def usage(self) -> RunUsage:
        return self._usage

    @property
    def call_store(self) -> ModelCallStore:
        """Expose only the call-metadata protocol to sibling deterministic tool code."""

        return self._store

    async def call(
        self,
        *,
        operation: ModelOperation,
        response_model: type[OutputT],
        instructions: str,
        payload: Mapping[str, object],
        run_id: str,
        incident_id: str,
        logical_key: str,
    ) -> OutputT:
        input_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        if len(input_json) > self._settings.investigator_max_context_chars:
            raise InvestigatorBudgetExceeded("model context character budget exceeded")
        model = self._router.model_for(operation)
        last_error: Exception | None = None
        for attempt in range(1, self._settings.investigator_model_max_attempts + 1):
            estimated_input_tokens = math.ceil(len(input_json) / 4)
            self._check_budget(estimated_input_tokens)
            sequence = self._usage.model_calls + 1
            call_id = _stable_call_id(run_id, "model", operation.value, logical_key, sequence)
            started = time.perf_counter()
            try:
                result = await self._provider.complete(
                    model=model,
                    instructions=instructions,
                    input_json=input_json,
                    response_model=response_model,
                    max_output_tokens=self._settings.investigator_max_output_tokens_per_call,
                    timeout_seconds=self._settings.investigator_model_timeout_seconds,
                )
                input_tokens = result.input_tokens or estimated_input_tokens
                output_tokens = result.output_tokens or math.ceil(
                    len(result.output.model_dump_json()) / 4
                )
                cost = self._estimated_cost(input_tokens, output_tokens)
                duration = time.perf_counter() - started
                await self._record_attempt(
                    call_id=call_id,
                    run_id=run_id,
                    incident_id=incident_id,
                    operation=operation,
                    model=model,
                    status="succeeded",
                    attempt=attempt,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=cost,
                    duration_seconds=duration,
                    metadata={"response_id": result.response_id} if result.response_id else {},
                )
                self._add_usage(input_tokens, output_tokens, cost)
                self._observe(
                    operation, model, "succeeded", duration, input_tokens, output_tokens, cost
                )
                return result.output
            except InvestigatorBudgetExceeded:
                raise
            except Exception as error:
                last_error = error
                duration = time.perf_counter() - started
                await self._record_attempt(
                    call_id=call_id,
                    run_id=run_id,
                    incident_id=incident_id,
                    operation=operation,
                    model=model,
                    status="failed",
                    attempt=attempt,
                    input_tokens=estimated_input_tokens,
                    output_tokens=None,
                    estimated_cost_usd=self._estimated_cost(estimated_input_tokens, 0),
                    duration_seconds=duration,
                    error=error,
                )
                cost = self._estimated_cost(estimated_input_tokens, 0)
                self._add_usage(estimated_input_tokens, 0, cost)
                self._observe(
                    operation,
                    model,
                    "failed",
                    duration,
                    estimated_input_tokens,
                    0,
                    cost,
                )
                if attempt < self._settings.investigator_model_max_attempts:
                    delay = self._settings.investigator_model_retry_backoff_seconds * attempt
                    if delay:
                        await asyncio.sleep(delay)
        assert last_error is not None
        raise ProviderResponseError(
            f"{operation.value} failed after bounded provider attempts"
        ) from last_error

    def _check_budget(self, estimated_input_tokens: int) -> None:
        if self._usage.model_calls >= self._settings.investigator_max_model_calls:
            raise InvestigatorBudgetExceeded("model call budget exceeded")
        projected_tokens = (
            self._usage.input_tokens
            + self._usage.output_tokens
            + estimated_input_tokens
            + self._settings.investigator_max_output_tokens_per_call
        )
        if projected_tokens > self._settings.investigator_max_total_tokens:
            raise InvestigatorBudgetExceeded("token budget exceeded")
        projected_cost = self._usage.estimated_cost_usd + self._estimated_cost(
            estimated_input_tokens,
            self._settings.investigator_max_output_tokens_per_call,
        )
        if projected_cost > self._settings.investigator_max_estimated_cost_usd:
            raise InvestigatorBudgetExceeded("estimated cost budget exceeded")

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self._settings.investigator_input_cost_per_million_usd
            + output_tokens * self._settings.investigator_output_cost_per_million_usd
        ) / 1_000_000

    def _add_usage(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        self._usage = RunUsage(
            model_calls=self._usage.model_calls + 1,
            tool_calls=self._usage.tool_calls,
            input_tokens=self._usage.input_tokens + input_tokens,
            output_tokens=self._usage.output_tokens + output_tokens,
            estimated_cost_usd=self._usage.estimated_cost_usd + cost,
        )

    async def _record_attempt(
        self,
        *,
        call_id: str,
        run_id: str,
        incident_id: str,
        operation: ModelOperation,
        model: str,
        status: str,
        attempt: int,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost_usd: float | None,
        duration_seconds: float,
        metadata: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        await self._store.record_call(
            ModelCallRecord(
                id=call_id,
                run_id=run_id,
                incident_id=incident_id,
                kind="model",
                operation=operation.value,
                provider=self._provider.name,
                model=model,
                status=status,
                attempt=attempt,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost_usd,
                duration_seconds=duration_seconds,
                error_type=type(error).__name__ if error is not None else None,
                error_message=(redact_text(str(error))[:512] if error is not None else None),
                metadata=metadata or {},
                created_at=datetime.now(UTC),
            )
        )

    def _observe(
        self,
        operation: ModelOperation,
        model: str,
        outcome: str,
        duration: float,
        input_tokens: int,
        output_tokens: int,
        cost: float,
    ) -> None:
        if self._telemetry is not None:
            self._telemetry.metrics.observe_model_call(
                operation=operation.value,
                provider=self._provider.name,
                model=model,
                outcome=outcome,
                duration_seconds=duration,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
            )


def _stable_call_id(
    run_id: str,
    kind: str,
    operation: str,
    logical_key: str,
    sequence: int,
) -> str:
    canonical = f"{run_id}:{kind}:{operation}:{logical_key}:{sequence}"
    return f"CALL-{hashlib.sha256(canonical.encode()).hexdigest()[:24].upper()}"
