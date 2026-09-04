# ADR 0003: Local observability and correlation baseline

- Status: Accepted
- Date: 2026-09-02
- Scope: Milestone 1B / Stage 02 correlated metrics, logs, and traces

## Context

Milestone 1A provides a real synchronous checkout across four FastAPI processes and PostgreSQL.
Milestone 1B must make one request discoverable in independent metric, log, and trace stores without
changing checkout semantics, leaking secrets, or turning telemetry availability into an application
dependency. The local topology also needs to resemble a production signal pipeline while remaining
small enough for deterministic Compose smoke tests.

## Decision

### Instrumentation and propagation

Each application factory creates one lifecycle-owned telemetry runtime. OpenTelemetry instruments
FastAPI server spans, individual HTTPX clients, and the payment service's async SQLAlchemy engine.
The HTTPX choice avoids process-global monkey-patching and keeps injected clients deterministic in
unit tests. Only W3C Trace Context is propagated between services.

Every resource has `service.name`, `service.version`, and `deployment.environment`. The public
gateway returns `X-Trace-ID` while the existing `X-Request-ID` remains the application correlation
identifier. Neither identifier is used as a metric label.

OTLP export is enabled explicitly in Compose and disabled by the side-effect-free settings default.
Trace and log exporters use bounded background processors. The Collector is not part of service
readiness, so export outage cannot fail checkout. Pull-based `/metrics` remains available when OTLP
is disabled.

### Logs and metrics

Application request completion logs are one-line JSON. Their stable fields include severity,
service/version/environment, trace ID, span ID, request ID, and bounded HTTP metadata. Bodies and
arbitrary headers are never logged. Only explicitly supplied structured fields are serialized, with
recursive size bounds and credential-key/text redaction.

Each service owns a separate Prometheus registry. The RED metric families are:

- `demo_http_requests_total`;
- `demo_http_request_errors_total`;
- `demo_http_request_duration_seconds`;
- `demo_http_requests_in_progress`.

Labels are restricted to service identity, an allowlisted HTTP method, the framework's route
template, status class, and client/server error type. Raw paths, trace/request IDs, idempotency keys,
and business IDs are forbidden to prevent unbounded cardinality.

### Local backend topology

Applications send OTLP logs and traces to a central OpenTelemetry Collector. It forwards traces via
OTLP/gRPC to Tempo and logs via native OTLP/HTTP to Loki. Prometheus scrapes each `/metrics`
endpoint. Grafana provisions Prometheus, Loki, and Tempo data sources plus one RED dashboard. Loki's
actual OTLP resource label `service_name` is used in dashboards, documentation, and smoke queries.

Container tags are explicit. Backend data uses named local volumes and loopback-only published
ports. This is a reproducible developer topology, not a claim of multi-tenant or production storage
durability.

### Acceptance proof

The observability smoke captures Prometheus counter baselines, performs one unique checkout, and
reads `X-Trace-ID`. It polls with a fixed deadline until request and latency counters increase for
all four service routes, Loki returns JSON logs from all four services containing that trace ID, and
Tempo returns one trace containing resources for all four services. Time-based sleeps are not used
as evidence.

## Consequences

Benefits:

- the same trace is mechanically provable across every HTTP hop and PostgreSQL spans;
- logs remain searchable by trace and request ID without creating high-cardinality metric series;
- telemetry infrastructure may fail independently of the checkout availability path;
- configuration and dashboard queries are versioned alongside emitted labels.

Tradeoffs:

- Python OpenTelemetry log APIs remain less stable than trace APIs, so exact dependencies are pinned
  and exercised by the live smoke gate;
- single-binary Loki and Tempo with local storage are appropriate only for this bounded demo;
- custom Prometheus middleware deliberately owns RED names and labels instead of exposing all
  framework instrumentation dimensions;
- returning `X-Trace-ID` is a local demo aid and should be reviewed before exposing it across a
  public trust boundary.

## Deferred from Milestone 1B

Fault controls, alert rules, Alertmanager, incident ingestion, evidence adapters, AI, RAG,
frontend, Kubernetes, and cloud resources remain outside Milestone 1B. Milestone 1C subsequently
adds the first three under [ADR 0004](0004-controlled-fault-and-alert-boundary.md); the rest remain
deferred.
