# Architecture

## System shape

The repository is a monorepo with four layers:

1. Demo workload that produces realistic telemetry and controlled faults.
2. Observability stack that stores metrics, logs, traces, and alerts.
3. Incident platform that persists, investigates, and presents incidents.
4. Delivery and evaluation assets for repeatable local and cloud operation.

Milestone 1A–1C and implementation-plan Stages 2–9 are implemented today: a working checkout,
correlated telemetry, a controlled fault-to-alert demonstration, durable asynchronous incident
ingestion, deterministic evidence collection, an evidence-grounded LangGraph investigator,
versioned knowledge retrieval as supporting context, a React approval UI with a durable
approval pause, a versioned fault-scenario eval framework with deterministic grading,
and approved rollback with deterministic recovery verification. Stage 10 delivery assets
(Helm chart, Terraform plan-only modules, separated CI workflows) are validated but never
applied from a development prompt; Docker Compose remains the primary local path.

## Implemented Stage 05 slice

Each demo service is an independent FastAPI process with a narrow typed boundary:

```text
POST /checkout
  gateway
    └─ POST /orders
         order service
           ├─ POST /reservations → inventory service
           └─ POST /payments     → payment service → checkout_payments (PostgreSQL)
```

The gateway and order service own orchestration only. Inventory owns the configured Stage 01 SKU,
availability limit, and unit price, but is intentionally stateless. Payment is the sole durable
business boundary: it atomically inserts a confirmed transaction or returns the record already
associated with the caller's idempotency key. `GET /payments/{payment_id}` reads that durable
result through application behavior.

Every process exposes `/health/live` without dependency checks. `/health/ready` checks only direct
requirements: gateway checks order; order checks inventory and payment; inventory has none; payment
checks PostgreSQL; Incident API checks PostgreSQL and Redis. Compose uses those checks to order
startup after the Alembic migration succeeds. Worker health uses Celery's targeted ping.

Each FastAPI process owns an isolated telemetry runtime. FastAPI server spans extract W3C
`traceparent`; individually instrumented HTTPX clients create client spans and inject that context;
payment instruments its async SQLAlchemy engine through the synchronous facade. Every span and
OTLP log resource carries the same `service.name`, `service.version`, and
`deployment.environment` schema. The public gateway returns `X-Trace-ID` so the local smoke runner
can correlate backend evidence without making trace IDs Prometheus labels.

Request middleware emits JSON completion logs with trace/span/request IDs and fixed HTTP metadata.
It never records bodies, idempotency keys, arbitrary headers, or raw URL paths. A recursive bounded
redactor protects credential-like structured fields. Per-process Prometheus registries expose
traffic, errors, duration histograms, and in-progress saturation on `/metrics`; label policy is
limited to service, allowlisted method, framework route template, status class, and error type.

Applications export traces and logs asynchronously over OTLP to one Collector. The Collector sends
traces to Tempo and native OTLP logs to Loki. Prometheus pulls application metrics, while Grafana
provisions the three data sources and a RED dashboard. Collector/export availability is not a
readiness dependency: background export failure may lose telemetry but cannot fail checkout.

Payment owns five process-local faults (`slow_database`, `pool_exhaustion`,
`bad_deployment`, `cpu_saturation`, `high_error_rate`) and inventory owns one
(`inventory_timeout`); the original `slow_database` controller and routes are
preserved for compatibility. Runtime state always initializes
off and changes only through its authenticated, loopback-published control API when validated
settings explicitly allow fault injection in a development/test environment. A lock protects state
changes, and enabled delay faults wait a fixed configured bounded duration immediately before
persistence (pool 1.0s, bad-deploy 1.2s, inventory 1.5s, CPU 0.2s plus a 2k-iteration hash loop
with no host burn); `high_error_rate` instead returns a deterministic hash-based simulated
500 for roughly half the keys. Every payment request span is annotated with fault state, service version, and environment; state
changes/injections are structured log events. A fixed `demo_fault_enabled` gauge makes current
state visible without caller-controlled labels.

Prometheus evaluates `DemoPaymentHighLatency` from the real payment `/payments` histogram labels.
Alertmanager sends firing and resolved notifications to Incident API. A separate bounded in-memory
receiver remains only in the explicit `test-tools` Compose profile so its Milestone 1 validation
contract is preserved without pretending it is durable ingestion.

Incident API validates and normalizes the bounded webhook, computes repository-owned alert and
delivery fingerprints, and commits the canonical incident, immutable occurrence, audit history,
named investigation run, and queue outbox row in one PostgreSQL transaction. Unique constraints,
not process memory, serialize concurrent duplicate deliveries. After commit it publishes only the
incident ID to Redis/Celery and records queue acceptance before returning 202. Publication failure
is retained as `publish_failed` and returned as 503 so Alertmanager can retry the same deterministic
task safely.

The Stage 05 worker reloads both job and canonical incident by ID under a row lock and bounded
lease. It transitions `queued → investigating`, scopes one bounded UTC window and allowlisted
service set, then concurrently invokes fixed Prometheus, Loki, Tempo, and local deployment domain
operations. Each source persists independently, including explicit `empty`, `unavailable`,
`failed`, or `timed_out` outcomes. A completed run records `evidence_collected` with
`ai_executed=false` and leaves root cause and confidence empty. Persistence failure uses
deterministic exponential backoff and eventually a durable dead-letter state; late acknowledgement
and a Redis visibility timeout recover worker loss. Backend partial failure is evidence state, not
a reason to erase successful sources or fabricate a result.

Telemetry integrations have separate low-level clients and domain adapters. The low-level clients
own fixed backend paths, retry/timeout/streamed-response-size behavior, and response validation;
compressed responses are refused because the clients explicitly request identity encoding. The
domain surface accepts strict service/window/limit/trace-ID models and repository-owned template
enums; callers cannot pass PromQL, LogQL, TraceQL, SQL, shell, or arbitrary URLs. Effective settings
that change query semantics are part of the persisted parameters and stable identity. Results are
sanitized before an incident-owned stable evidence ID and payload hash are monotonically upserted:
a retry failure cannot replace an already collected or empty result. A deterministic timeline
expands known log, trace, and deployment payloads, derives event IDs from intrinsic content rather
than list positions, and sorts all events by UTC timestamp plus stable identities; it makes no
causal claim.

The Celery child owns one telemetry runtime for its process lifetime and serves its bounded adapter
and collection registry on port 9464. Prometheus scrapes that internal target, while Compose
publishes it on loopback for local verification.

Deployment history is an immutable local PostgreSQL registry exposed through typed Incident API
registration and list endpoints. The worker reads only named recent/current/previous operations.
No GitHub client or external credentials are present.

Shared strict Pydantic contracts live in `packages/models`, environment-backed settings in
`packages/config`, and SQLAlchemy metadata/store code in `packages/persistence`. Application HTTP
clients are injected behind protocols so unit and contract tests replace network hops with fakes;
the Compose path uses real HTTP.

## Target repository structure

```text
ai-sre-platform/
├── apps/
│   ├── demo/
│   │   ├── gateway/
│   │   ├── order_service/
│   │   ├── inventory_service/
│   │   ├── payment_service/
│   │   └── alert_receiver/
│   ├── incident_api/
│   ├── investigator_worker/
│   └── frontend/
├── packages/
│   ├── agents/
│   ├── tools/
│   ├── rag/
│   ├── models/
│   ├── incidents/
│   ├── persistence/
│   ├── task_queue/
│   └── telemetry/
├── observability/
│   ├── prometheus/
│   ├── alertmanager/
│   ├── grafana/
│   ├── loki/
│   ├── tempo/
│   └── otel_collector/
├── infrastructure/
│   ├── docker/
│   ├── helm/
│   └── terraform/
├── docs/
│   ├── runbooks/
│   ├── adr/
│   ├── PRODUCT_REQUIREMENTS.md
│   ├── ARCHITECTURE.md
│   ├── DOMAIN_AND_API.md
│   ├── IMPLEMENTATION_PLAN.md
│   ├── QUALITY_GATES.md
│   ├── SECURITY.md
│   └── EVALS.md
├── evals/
│   ├── datasets/
│   └── scenarios/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── agent/
│   └── e2e/
├── scripts/
├── docker-compose.yml
├── Makefile
├── pyproject.toml
├── uv.lock
└── README.md
```

Exact package names may be adjusted during bootstrap, but architectural boundaries and import direction must remain clear.

## Target runtime data flow

```text
client
  → gateway
  → order-service
  → inventory-service
  → payment-service
  → PostgreSQL

services
  → OTLP traces/logs → OpenTelemetry Collector → Tempo/Loki
  → /metrics         → Prometheus

current Stage 05 path:
Prometheus → Alertmanager → Incident API → PostgreSQL
                                      └→ Redis/Celery → Investigator Worker
                                                            ├→ Prometheus
                                                            ├→ Loki
                                                            ├→ Tempo
                                                            └→ deployment registry
                                                                   ↓
                                                         canonical evidence → PostgreSQL

later path:
Investigator Worker
  → existing allowlisted evidence adapters + later knowledge adapter
  → LangGraph workflow
  → structured IncidentReport → PostgreSQL

Frontend → Incident API
Human approval → allowlisted remediation adapter → demo control endpoint
```

## Key boundaries

### Demo services

They are intentionally small. Their purpose is to provide realistic service-to-service behavior and telemetry, not model a full e-commerce domain. Fault injection is separated from normal business logic behind explicit configuration or authenticated local-only control endpoints.

In Stage 01, callers provide a bounded `Idempotency-Key` and an optional safe `X-Request-ID`.
Service clients use explicit timeouts and a maximum of three attempts. Retried POSTs preserve both
headers; therefore only the payment database boundary decides whether a result is new, an exact
replay, or a conflicting key reuse. Milestone 1B additionally propagates W3C trace context across
the same clients. Milestone 1C adds only the guarded `slow_database` control at the payment
boundary; no general-purpose fault executor exists.

### Telemetry adapters

Each adapter has two layers:

- low-level HTTP client with timeouts, retries, response validation, and error mapping;
- domain methods with fixed query templates and bounded inputs.

The current deterministic worker receives only domain methods such as `get_service_latency`, never
a generic arbitrary query capability. The same boundary will constrain later model orchestration.

### Incident API and worker

The API is latency-sensitive and performs no AI work inline. The queue message carries an incident
ID, not the full alert payload. An outbox row is committed with canonical state before bounded
broker publication. A repeated webhook can safely republish a `pending_publish`/`publish_failed`
job with the same task ID.

The worker reloads canonical state from PostgreSQL, acquires an idempotency/lease guard, and
persists each transition. Stage 05 registers `incident.collect_evidence`; a legacy Stage 04 task
name remains only to safely consume already-published messages. The task concurrently collects and
persists deterministic evidence, records a no-AI completion, and stops. The opt-in LangGraph
investigator (Stage 06) persists schema-validated reports, and versioned knowledge retrieval
(Stage 07) supplies supporting historical context with `KNW-` citations.

### Frontend and human approval

Stage 08 adds a static React review UI served by nginx on `FRONTEND_PORT` with a same-origin
`/api` proxy to incident-api and a `/health/live` healthcheck. The list and detail views read
only from the Incident API (incidents, report, hypotheses, recommendations, evidence and
evidence timeline, audit timeline, knowledge chunks); fixtures exist solely in frontend test
assets. Approve/reject is API-owned: `POST recommendations/{id}/approve|reject` validates the
incident version and demo actor, requires `Idempotency-Key`, serializes concurrent decisions
with row locks plus a one-decision-per-recommendation constraint, replays idempotently, and
audits every outcome. Approval records the decision without executing remediation (the
incident pause in `waiting_for_approval` persists); rejection moves the incident to
`rejected`. The graph is not re-entered in this stage.

### LangGraph workflow

Planned Stage 4 baseline graph:

```text
START
  → scope incident
  → collect metrics/logs/traces/deployments
  → correlate
  → generate report
  → END
```

Target graph:

```text
START
  → plan
  → parallel evidence workers
  → correlate timeline
  → retrieve related knowledge
  → generate competing hypotheses
  → verify each hypothesis
  → evidence sufficient?
      no  → bounded additional collection → verify
      yes → final RCA
  → recommendations
  → WAITING_FOR_APPROVAL
```

The loop has a configured maximum iteration count and tool-call budget. Checkpointing enables restart and human approval without replaying completed work.

### RAG

Stage 07 retrieves versioned Markdown knowledge only after current telemetry
correlation and before hypothesis generation/verification. Documents carry source
path/type, version, SHA-256 hash, timestamps, metadata, and chunk order;
re-ingestion is idempotent by path and content hash, updates replace chunks, and
removal deletes the document and its chunks. Chunking defaults to 600 tokens with
100 overlap; embeddings are behind a provider interface (deterministic fake
offline, OpenAI opt-in) with dimensions validated against configuration (1536).
Retrieval uses metadata filters, bounded `top_k` (default 8), cosine similarity,
and an HNSW index. Allowlisted methods cover runbooks, prior incidents, and
architecture documents. Retrieved text is delimited untrusted context with size
limits; instructions inside it cannot alter tools, policy, or workflow.
Historical similarity is cited with `KNW-` IDs distinct from current `EVD-`
evidence and never proves the current root cause.

## Reliability decisions

- Stage 01 checkout retries are idempotent at the payment persistence boundary.
- All Stage 01 network calls have explicit timeouts, bounded attempts, validated responses, and
  deterministic error mapping.
- Telemetry exporters use bounded background batches and are excluded from application readiness,
  so Collector failure cannot break checkout.
- Metric labels use code-owned route templates and fixed value sets; trace, request, business, and
  idempotency identifiers never become metric labels.
- Alert ingestion uses a stable SHA-256 fingerprint over canonical sorted labels for deduplication.
- Repeated delivery of the same firing/resolved update is a no-op; a real state edge appends one
  occurrence. PostgreSQL uniqueness and row locks cover concurrent API processes.
- Evidence collection records failures per source and permits partial results.
- The investigator never converts missing telemetry into negative evidence.
- All investigation steps can be retried safely.
- Queue retries use exponential backoff and a dead-letter/failure state.
- Workflow and remediation actions write audit events.
- Recovery verification uses deterministic telemetry thresholds, not only LLM judgment.

## Model routing

Model names are configuration, not hard-coded domain behavior. A lower-cost configured model may handle classification/summarization; the strongest configured model handles final hypothesis evaluation and RCA. Tests use a fake model provider. Production code must expose token, latency, error, and estimated-cost telemetry per logical operation.

## Deployment order

1. Docker Compose local stack.
2. Reproducible E2E demo.
3. Kubernetes manifests and Helm (`infrastructure/helm/ai-sre-platform/`;
   single source of truth, validated by `make k8s-validate`).
4. Terraform for GCP resources (`infrastructure/terraform/`; plan-only,
   never auto-applied).
5. CI/CD deployment and post-deploy E2E (`.github/workflows/`;
   `platform.yml` validates+scans, `deploy.yml` builds/pushes/deploys dev
   behind environment protection, `eval-live.yml` stays manual+budget-gated).

Cloud infrastructure is not allowed to complicate or block the local implementation.
