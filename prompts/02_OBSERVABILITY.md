# Stage 02 — correlated metrics, logs, and traces

Implement only Milestone 1B. Read `AGENTS.md`, architecture, plan, quality gates, and the current code. Preserve the working checkout flow.

Add production-shaped local observability:

- OpenTelemetry SDK/instrumentation for FastAPI, HTTPX, and relevant DB operations;
- W3C context propagation across every service hop;
- resource attributes including `service.name`, `service.version`, and `deployment.environment`;
- structured JSON logs containing `trace_id`, `span_id`, service, version, severity, and request/correlation ID when available;
- Prometheus request count, error count/rate source data, and latency histogram exposed on `/metrics` without uncontrolled label cardinality;
- OpenTelemetry Collector as the central OTLP receiver for logs/traces;
- Tempo for traces, Loki for logs, Prometheus for metrics, and Grafana with provisioned data sources and a useful RED dashboard;
- documented local URLs and example queries that use the actual labels emitted by this repository.

Ensure telemetry export failure does not break checkout. Do not log request secrets or high-cardinality arbitrary payloads. Pin container versions; do not use floating `latest` tags.

Add tests for propagation helpers, log enrichment/redaction, metric label policy, and telemetry-disabled behavior. Add an automated smoke script that performs one checkout, obtains the trace ID, and proves through APIs that corresponding metrics, logs, and a multi-service trace exist. Avoid timing-flaky sleeps; poll with a bounded deadline and diagnostic output.

Do not implement faults, alerts, Incident API, AI, RAG, frontend, Kubernetes, or cloud resources.

Validate from a clean Compose start. Run applicable quality gates and report concrete proof for metric → log → trace correlation. Do not commit or start Stage 03.
