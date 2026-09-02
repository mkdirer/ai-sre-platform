"""Lifecycle-owned OpenTelemetry and Prometheus runtime."""

import logging
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy.ext.asyncio import AsyncEngine

from packages.config import Settings
from packages.telemetry.logging import JsonLogFormatter
from packages.telemetry.metrics import HttpMetrics

_EXCLUDED_SERVER_URLS = r".*/health/(live|ready)$,.*/metrics$"


@dataclass(frozen=True)
class ServiceIdentity:
    """Stable telemetry resource and label identity."""

    name: str
    version: str
    environment: str


class TelemetryRuntime:
    """Own providers, handlers, and instrumentation for one service process."""

    def __init__(self, *, identity: ServiceIdentity, settings: Settings) -> None:
        self.identity = identity
        self.metrics = HttpMetrics(identity.name)
        self._formatter = JsonLogFormatter(
            service_name=identity.name,
            service_version=identity.version,
            environment=identity.environment,
        )
        self.logger = self._build_console_logger(settings)
        self.tracer_provider: TracerProvider | None = None
        self.logger_provider: LoggerProvider | None = None
        self._closed = False

        if settings.telemetry_enabled:
            self._configure_otel(settings)

    @classmethod
    def create(cls, *, service_name: str, settings: Settings) -> "TelemetryRuntime":
        """Build a runtime from validated settings without making network calls."""

        identity = ServiceIdentity(
            name=service_name,
            version=settings.service_version,
            environment=settings.environment.value,
        )
        return cls(identity=identity, settings=settings)

    def _build_console_logger(self, settings: Settings) -> logging.Logger:
        logger = logging.Logger(f"ai_sre.{self.identity.name}", level=settings.log_level.value)
        logger.propagate = False
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self._formatter)
        logger.addHandler(console_handler)
        return logger

    def _configure_otel(self, settings: Settings) -> None:
        endpoint = str(settings.otel_exporter_otlp_endpoint).rstrip("/")
        export_timeout = settings.otel_export_timeout_seconds
        schedule_delay = settings.otel_batch_schedule_delay_milliseconds
        resource = Resource.create(
            {
                "service.name": self.identity.name,
                "service.version": self.identity.version,
                "deployment.environment": self.identity.environment,
            }
        )

        try:
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=endpoint,
                        insecure=endpoint.startswith("http://"),
                        timeout=export_timeout,
                    ),
                    schedule_delay_millis=schedule_delay,
                    export_timeout_millis=export_timeout * 1_000,
                )
            )
            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(
                        endpoint=endpoint,
                        insecure=endpoint.startswith("http://"),
                        timeout=export_timeout,
                    ),
                    schedule_delay_millis=schedule_delay,
                    export_timeout_millis=export_timeout * 1_000,
                )
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.logger.warning(
                "telemetry.initialization_failed",
                extra={"structured": {"error.type": type(error).__name__}},
            )
            return

        propagate.set_global_textmap(TraceContextTextMapPropagator())
        otel_handler = LoggingHandler(level=self.logger.level, logger_provider=logger_provider)
        otel_handler.setFormatter(self._formatter)
        self.logger.addHandler(otel_handler)
        self.tracer_provider = tracer_provider
        self.logger_provider = logger_provider

    @property
    def enabled(self) -> bool:
        """Whether OTLP trace/log exporting was configured successfully."""

        return self.tracer_provider is not None and self.logger_provider is not None

    def instrument_fastapi(self, app: FastAPI) -> None:
        """Instrument one FastAPI app when OTLP telemetry is enabled."""

        if self.tracer_provider is None:
            return
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            excluded_urls=_EXCLUDED_SERVER_URLS,
            exclude_spans=["receive", "send"],
        )

    def instrument_httpx_client(self, client: httpx.AsyncClient) -> None:
        """Instrument a single outbound HTTPX client without global monkey-patching."""

        if self.tracer_provider is None:
            return
        HTTPXClientInstrumentor.instrument_client(
            client,
            tracer_provider=self.tracer_provider,
        )

    def instrument_sqlalchemy_engine(self, engine: AsyncEngine) -> None:
        """Instrument relevant async SQLAlchemy operations through its sync facade."""

        if self.tracer_provider is None:
            return
        SQLAlchemyInstrumentor().instrument(
            engine=engine.sync_engine,
            tracer_provider=self.tracer_provider,
        )

    def shutdown(self) -> None:
        """Flush bounded exporter queues during graceful process shutdown."""

        if self._closed:
            return
        self._closed = True
        if self.logger_provider is not None:
            self.logger_provider.shutdown()
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


__all__ = ["ServiceIdentity", "TelemetryRuntime"]
