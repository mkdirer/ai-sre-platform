"""Environment-backed, side-effect-free application settings."""

from enum import StrEnum
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Supported deployment environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Supported application log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Shared settings loaded from environment variables or a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO
    service_version: Annotated[
        str,
        StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$"),
    ] = "0.1.0"
    telemetry_enabled: bool = False
    otel_exporter_otlp_endpoint: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:4317")
    otel_export_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 2.0
    otel_batch_schedule_delay_milliseconds: Annotated[int, Field(ge=100, le=5_000)] = 500
    postgres_host: str = "127.0.0.1"
    postgres_port: Annotated[int, Field(ge=1, le=65_535)] = 5432
    postgres_db: str = "aisre"
    postgres_user: str = "aisre"
    postgres_password: SecretStr = Field(default=SecretStr(""), repr=False)
    celery_broker_url: SecretStr = Field(
        default=SecretStr("redis://127.0.0.1:6379/0"),
        repr=False,
    )
    celery_result_backend_url: SecretStr = Field(
        default=SecretStr("redis://127.0.0.1:6379/1"),
        repr=False,
    )
    queue_publish_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 2.0
    investigation_max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    investigation_retry_base_seconds: Annotated[int, Field(ge=1, le=300)] = 2
    investigation_retry_max_seconds: Annotated[int, Field(ge=1, le=3_600)] = 30
    investigation_job_lease_seconds: Annotated[int, Field(ge=5, le=3_600)] = 15
    celery_visibility_timeout_seconds: Annotated[int, Field(ge=30, le=86_400)] = 300
    prometheus_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:9090")
    loki_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:3100")
    tempo_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:3200")
    evidence_http_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 2.0
    evidence_http_max_attempts: Annotated[int, Field(ge=1, le=3)] = 2
    evidence_http_retry_backoff_seconds: Annotated[float, Field(ge=0, le=1)] = 0.05
    evidence_source_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 8.0
    evidence_max_response_bytes: Annotated[int, Field(ge=1_024, le=10_485_760)] = 2_097_152
    evidence_max_window_seconds: Annotated[int, Field(ge=60, le=21_600)] = 3_600
    evidence_max_lookback_seconds: Annotated[int, Field(ge=300, le=86_400)] = 21_600
    evidence_future_skew_seconds: Annotated[int, Field(ge=0, le=900)] = 300
    evidence_deployment_lookback_seconds: Annotated[int, Field(ge=300, le=86_400)] = 7_200
    evidence_metric_step_seconds: Annotated[int, Field(ge=1, le=300)] = 5
    evidence_log_limit: Annotated[int, Field(ge=1, le=100)] = 50
    evidence_trace_limit: Annotated[int, Field(ge=1, le=20)] = 10
    evidence_deployment_limit: Annotated[int, Field(ge=1, le=50)] = 10
    evidence_correlation_limit: Annotated[int, Field(ge=100, le=5_000)] = 1_000
    evidence_slow_trace_threshold_ms: Annotated[int, Field(ge=1, le=60_000)] = 500
    investigator_enabled: bool = False
    investigator_provider: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,31}$"),
    ] = "openai"
    investigator_planning_model: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ] = "gpt-5-mini"
    investigator_reasoning_model: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ] = "gpt-5-mini"
    openai_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    investigator_model_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    investigator_model_max_attempts: Annotated[int, Field(ge=1, le=3)] = 2
    investigator_model_retry_backoff_seconds: Annotated[float, Field(ge=0, le=5)] = 0.25
    investigator_max_output_tokens_per_call: Annotated[int, Field(ge=64, le=16_384)] = 2_048
    investigator_max_model_calls: Annotated[int, Field(ge=1, le=50)] = 16
    investigator_max_tool_calls: Annotated[int, Field(ge=12, le=50)] = 16
    investigator_max_iterations: Annotated[int, Field(ge=1, le=5)] = 2
    investigator_max_context_chars: Annotated[int, Field(ge=2_048, le=200_000)] = 30_000
    investigator_max_total_tokens: Annotated[int, Field(ge=1_000, le=1_000_000)] = 50_000
    investigator_max_duration_seconds: Annotated[float, Field(gt=1, le=600)] = 120.0
    investigator_max_estimated_cost_usd: Annotated[float, Field(ge=0, le=100)] = 2.0
    investigator_input_cost_per_million_usd: Annotated[float, Field(ge=0, le=1_000)] = 0.0
    investigator_output_cost_per_million_usd: Annotated[float, Field(ge=0, le=1_000)] = 0.0
    investigator_root_confidence_threshold: Annotated[float, Field(ge=0.5, le=0.95)] = 0.65
    investigator_min_competing_hypotheses: Annotated[int, Field(ge=3, le=5)] = 3
    knowledge_provider: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,31}$"),
    ] = "fake"
    knowledge_embedding_model: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ] = "text-embedding-3-small"
    knowledge_embedding_dimensions: Annotated[int, Field(ge=8, le=4096)] = 1536
    knowledge_chunk_tokens: Annotated[int, Field(ge=100, le=2000)] = 600
    knowledge_chunk_overlap_tokens: Annotated[int, Field(ge=0, le=500)] = 100
    knowledge_top_k: Annotated[int, Field(ge=1, le=20)] = 8
    knowledge_max_top_k: Annotated[int, Field(ge=1, le=50)] = 20
    knowledge_max_context_chars: Annotated[int, Field(ge=1_024, le=50_000)] = 6_000
    knowledge_max_chunk_chars: Annotated[int, Field(ge=256, le=8_000)] = 2_000
    knowledge_embedding_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    outbound_http_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    outbound_http_max_attempts: Annotated[int, Field(ge=1, le=3)] = 2
    outbound_http_retry_backoff_seconds: Annotated[float, Field(ge=0, le=1)] = 0.05
    database_connect_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 3.0
    order_service_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8002")
    inventory_service_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8003")
    payment_service_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8004")
    inventory_sku: str = "widget-001"
    inventory_stock: Annotated[int, Field(ge=1, le=100_000)] = 100
    inventory_unit_price_cents: Annotated[int, Field(ge=1, le=10_000_000)] = 1999
    fault_injection_allowed: bool = False
    fault_control_token: SecretStr = Field(default=SecretStr(""), repr=False)
    slow_database_delay_seconds: Annotated[float, Field(ge=2.0, le=3.0)] = 2.5
    pool_exhaustion_delay_seconds: Annotated[float, Field(ge=0.5, le=2.0)] = 1.0
    bad_deployment_delay_seconds: Annotated[float, Field(ge=0.5, le=2.0)] = 1.2
    inventory_timeout_delay_seconds: Annotated[float, Field(ge=1.0, le=2.0)] = 1.5
    cpu_saturation_delay_seconds: Annotated[float, Field(ge=0.05, le=0.5)] = 0.2
