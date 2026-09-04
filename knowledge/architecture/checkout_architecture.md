# Checkout Architecture

.models/type: architecture
service: payment-service
version: v1

## Request path

`POST /checkout` enters the gateway, which creates a deterministic order ID from
the `Idempotency-Key` header and calls `POST /orders`. The order service fans out
to `POST /reservations` on the stateless inventory service and `POST /payments`
on the payment service. Only the payment service persists to PostgreSQL
(`checkout_payments`) with idempotent insert-or-read semantics.

## Telemetry

Each service owns an isolated OpenTelemetry runtime with W3C `traceparent`
propagation, JSON logs carrying `trace_id`, `span_id`, `request_id`, and
`incident_id`, and Prometheus RED metrics with fixed service/method/route
labels. Traces flow to Tempo, logs to Loki, metrics to Prometheus.

## Deployment

All demo services share one locked container image built from
`infrastructure/docker/demo.Dockerfile`. PostgreSQL uses the pgvector image in
Stage 07 to support cosine similarity over knowledge embeddings. The
investigator worker retrieves historical knowledge only after current telemetry
correlation and before hypothesis generation.

## Boundaries

The model may only use allowlisted domain methods. It must never generate
PromQL, LogQL, SQL, shell, or Kubernetes commands. Knowledge citations use
`KNW-` chunk IDs and are distinct from current `EVD-` evidence IDs.
