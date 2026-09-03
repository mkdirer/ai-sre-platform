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
