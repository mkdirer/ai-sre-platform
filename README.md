# AI SRE Incident Investigation Platform

This repository is being built into an evidence-grounded incident investigation platform. The
current vertical slice is **Stage 09 / implementation-plan Stage 8**: Milestone 1 and the
Stage 05 deterministic evidence run remain working, an opt-in LangGraph investigator turns
canonical evidence into a schema-validated report with deterministic grounding, versioned
knowledge retrieval provides supporting historical context with pgvector, a small React
frontend reviews incidents and records human approve/reject decisions without executing
remediation, and a reproducible eval framework grades seven core fault scenarios plus five
edge fixtures offline with scripted providers.

The implemented request path is:

```text
client → gateway → order service → inventory service → payment service → PostgreSQL
                  └─ Prometheus scrapes each service's /metrics
services → OTLP logs/traces → OpenTelemetry Collector → Loki / Tempo
Prometheus + Loki + Tempo → provisioned Grafana RED dashboard
Prometheus → Alertmanager → Incident API → PostgreSQL
                                      └→ Redis/Celery → investigator worker
                                                           ├→ Prometheus
                                                           ├→ Loki
                                                           ├→ Tempo
                                                           └→ local deployment store
                                                                  ↓
                                                        canonical evidence → PostgreSQL
```

The gateway accepts `POST /checkout`. Every HTTP hop carries the same `X-Request-ID` and W3C
`traceparent`; a required
`Idempotency-Key` yields a deterministic order and reservation and an atomic payment insert. An
exact retry returns the original payment with `idempotent_replay=true`; reuse of the key with a
different payload returns HTTP 409. The payment API's `GET /payments/{payment_id}` exposes a
read-back path for persistence verification. Successful instrumented responses also expose the
32-character trace ID in `X-Trace-ID` for local correlation.

See the [product requirements](docs/PRODUCT_REQUIREMENTS.md),
[architecture](docs/ARCHITECTURE.md), [implementation plan](docs/IMPLEMENTATION_PLAN.md), and
[demo API contract](docs/DOMAIN_AND_API.md).

## Current scope

Implemented now:

- Python 3.12 monorepo managed by `uv`, with locked runtime/development dependencies;
- FastAPI gateway, order, inventory, and payment applications;
- explicit outbound HTTP timeouts, bounded retries, correlation, and stable error envelopes;
- async SQLAlchemy 2 payment persistence and an Alembic migration;
- liveness and dependency-scoped readiness endpoints;
- dependency-ordered Docker Compose services and a reusable non-root application image;
- unit, in-process service contract, migration, durable-store, and live-chain tests;
- a bounded smoke/load runner with persistence and idempotent-retry validation;
- OpenTelemetry FastAPI, HTTPX, and SQLAlchemy instrumentation with W3C propagation;
- JSON service logs with trace/span/request correlation and secret-safe bounded attributes;
- bounded-cardinality request, error, latency, and in-progress Prometheus metrics;
- a central OpenTelemetry Collector, Tempo, Loki, Prometheus, and provisioned Grafana RED dashboard;
- a bounded observability smoke that proves one checkout through backend APIs;
- an authenticated, local-only, reversible `slow_database` payment fault that always starts off;
- a real payment-latency alert, pinned Alertmanager, and bounded in-memory webhook receiver stub;
- durable normalized firing/resolved ingestion with stable fingerprinting and database-enforced
  deduplication under repeated or concurrent delivery;
- typed, paginated Incident API reads, audit timeline, run/job visibility, and structured logs with
  `incident_id`;
- Redis and a non-root Celery worker whose JSON message carries only `incident_id`;
- typed, async, read-only Prometheus, Loki, Tempo, and local deployment adapters behind fixed
  domain operations rather than arbitrary backend query strings;
- bounded incident scoping, per-source concurrent collection, explicit partial-failure states,
  idempotent stable evidence IDs, sanitized payloads, and deterministic timeline correlation;
- typed deployment registration/listing and incident-isolated evidence/timeline API endpoints;
- an explicit `evidence_collection` run, idempotent worker lease, bounded exponential retry,
  durable dead-letter state, and visible publish failures;
- an opt-in (`INVESTIGATOR_ENABLED`) checkpointed LangGraph investigator that turns canonical
  evidence into a schema-validated `IncidentReport` via OpenAI Structured Outputs, with
  deterministic grounding/validation, persisted hypotheses/calls/failures, model/tool budget
  enforcement, and investigator metrics; deterministic agent tests use scripted fakes
  (`make demo-investigation-report` shows a fixture report without credentials);
- a cleanup-safe E2E scenario that proves fault, alert, one durable incident, evidence from all
  four local sources, duplicate no-op, disable, and resolved update.
- versioned Markdown knowledge (runbooks, architecture, known issues, prior incidents) with
  deterministic chunking, hash-derived fake embeddings offline, pgvector cosine retrieval with
  HNSW, allowlisted runbook/prior-incident/architecture methods, `KNW-` citations distinct from
  current `EVD-` evidence, untrusted-content delimiting, and retrieval after telemetry correlation
  (`make ingest-knowledge`, `GET /api/v1/knowledge/search`).
- a small React + TypeScript + Vite frontend (`apps/frontend`, served from Compose on
  `FRONTEND_PORT` with same-origin `/api` proxy and `/health/live`) with incident list and
  detail views: severity/service/status/timestamps plus confidence/data-gap indicator; RCA or
  insufficient-evidence state; correlated timeline; evidence cards with provenance;
  competing hypotheses with rejected reasons; related knowledge; recommendation risk; audit
  status; and approve/reject controls shown only for `waiting_for_approval`
  recommendations (`POST /api/v1/recommendations/{id}/approve|reject` with
  `incident_version`, demo `actor`, and `Idempotency-Key`; stale/conflicting requests return
  actionable 409s; approval records the decision and resumes state without executing
  remediation).

Explicitly deferred are remediation,
Kubernetes, and cloud resources. The Stage 03 receiver remains available only in the `test-tools`
Compose profile and its contract test; Alertmanager no longer targets it.

## Reproducible evals (Stage 09)

Twelve versioned scenarios live in `evals/scenarios/*.json` (`v1` core SCN-001..007,
`v1-extended` adds SCN-008..012 edge fixtures). Faults are allowlisted, bounded, reversible,
and auto-cleaned: `slow_database`, `pool_exhaustion`, `bad_deployment`,
`inventory_timeout`, simulated `cpu_saturation`, deterministic `high_error_rate`, and
`healthy`. CPU pressure is a short simulated delay, never host burn.

Run the deterministic offline suite (CI, no credentials, no cost):

```bash
make eval-fake
make eval-extended
make test-eval
```

Artifacts land in `evals/results/eval-<dataset>.{json,md}` with dataset version, Git commit,
model config, per-scenario grades, and aggregate metrics. The README shows no
accuracy numbers here; read the generated artifacts instead.

The optional live runner is gated and bounded:

```bash
RUN_LIVE_EVALS=1 EVAL_LIVE_CONFIRM=1 EVAL_MAX_COST_USD=0.5 make eval-live
```

It refuses to run without both flags and a positive cost budget, generates at most 12
checkouts per scenario, and always attempts fault cleanup. Paid model evals are not run
without explicit approval. See [ADR 0010](docs/adr/0010-eval-scenarios-and-grading-boundary.md)
and `evals/datasets/v1/README.md`.

## Prerequisites and setup

- Python 3.12;
- [`uv`](https://docs.astral.sh/uv/) 0.12.9;
- Docker Engine with the Compose v2 plugin;
- GNU Make (recommended; direct commands are shown where acceptance evidence matters).

From the repository root:

```bash
make setup
```

This runs `uv sync --all-groups --locked` and refuses to rewrite a stale lockfile. Local overrides
are optional:

```bash
cp .env.example .env
```

The checked-in password is for an isolated local demo only. Replace `POSTGRES_PASSWORD` outside
that context. `.env` is ignored, and secret-valued settings use `SecretStr` so normal
representations do not disclose them.

## Start, migrate, and stop

Build the application image, start PostgreSQL and Redis, apply migrations, and start the four demo
services, Incident API, Celery worker, and local observability/alert stack:

```bash
make compose-up
docker compose ps
```

`make compose-up` runs `docker compose up --build -d`. Compose waits for PostgreSQL health, runs
the one-shot `migrate` service to `alembic upgrade head`, then starts database consumers in
dependency-health order. Incident API readiness checks only PostgreSQL and Redis. Inventory has no
external readiness dependency.

Tempo and Loki start before the Collector; Prometheus scrapes the FastAPI services and Grafana
waits for its three data sources. Alertmanager waits for Incident API health. Telemetry is
deliberately absent from application readiness:
Collector/export failure cannot make checkout unavailable, and bounded background exporters retry
without blocking the request path.

To run the migration explicitly against the running Compose database:

```bash
make migrate
```

To stop services while retaining database data:

```bash
make compose-down
```

For an explicitly clean migration test, remove only this Compose project's containers and named
volume, then start it again:

```bash
docker compose down --volumes --remove-orphans
make compose-up
```

If the optional receiver profile was started, include the profile when removing it:

```bash
docker compose --profile test-tools down --volumes --remove-orphans
```

## Checkout and persistence verification

Submit a checkout with caller-supplied correlation and idempotency headers:

```bash
curl --fail-with-body \
  --request POST http://127.0.0.1:8001/checkout \
  --header 'Content-Type: application/json' \
  --header 'X-Request-ID: readme-checkout-1' \
  --header 'Idempotency-Key: readme-checkout-1' \
  --data '{"customer_id":"customer-1","sku":"widget-001","quantity":2}'
```

Copy the returned `payment_id` and read the durable result through the payment API:

```bash
curl --fail-with-body \
  --header 'X-Request-ID: readme-payment-read-1' \
  http://127.0.0.1:8004/payments/REPLACE_WITH_PAYMENT_ID
```

Repeat the original checkout command unchanged to receive the same payment/order IDs and
`"idempotent_replay":true`. Changing its body while reusing the same idempotency key returns 409.

The bounded checkout smoke runner performs these checks automatically and prints every checkout,
persistence-read, and replay request ID:

```bash
make smoke
SMOKE_REQUEST_COUNT=5 make smoke
```

`SMOKE_REQUEST_COUNT` is restricted to 1–20. `SMOKE_GATEWAY_URL` and `SMOKE_PAYMENT_URL` override
the default localhost endpoints.

## Metrics, logs, traces, and Grafana

| Component | Local URL |
| --- | --- |
| Gateway metrics | <http://127.0.0.1:8001/metrics> |
| Order metrics | <http://127.0.0.1:8002/metrics> |
| Inventory metrics | <http://127.0.0.1:8003/metrics> |
| Payment metrics | <http://127.0.0.1:8004/metrics> |
| Incident API / metrics | <http://127.0.0.1:8006/docs> / <http://127.0.0.1:8006/metrics> |
| Investigator metrics | <http://127.0.0.1:9464/metrics> |
| Prometheus | <http://127.0.0.1:9090> |
| Loki API | <http://127.0.0.1:3100> |
| Tempo API | <http://127.0.0.1:3200> |
| Collector health | <http://127.0.0.1:13133> |
| Grafana | <http://127.0.0.1:3000> |
| Provisioned RED dashboard | <http://127.0.0.1:3000/d/demo-services-red> |
| Alertmanager | <http://127.0.0.1:9093> |

Grafana allows anonymous local Viewer access. Its provisioned data sources are named `Prometheus`,
`Loki`, and `Tempo`. Application resources consistently emit `service.name`, `service.version`,
and `deployment.environment`; Loki promotes `service.name` to the actual stream label
`service_name`. Useful queries using the repository's emitted names and labels are:

```promql
sum by (service) (
  rate(demo_http_requests_total{route!~"/health/.*|/metrics"}[1m])
)

sum by (service) (
  rate(demo_http_request_errors_total{error_type="server"}[5m])
)

histogram_quantile(
  0.95,
  sum by (le, service) (
    rate(demo_http_request_duration_seconds_bucket{route!~"/health/.*|/metrics"}[5m])
  )
)

sum by (service) (demo_http_requests_in_progress)
```

```logql
{service_name=~"gateway|order-service|inventory-service|payment-service"}
  |= "REPLACE_WITH_X_TRACE_ID"
  | json
```

Retrieve the same trace directly from Tempo with:

```bash
curl --fail-with-body http://127.0.0.1:3200/api/traces/REPLACE_WITH_X_TRACE_ID
```

The automated proof captures metric baselines, performs exactly one unique checkout, reads its
`X-Trace-ID`, and polls each backend with a fixed deadline until all four service metrics, all four
correlated log streams, and one multi-service trace exist:

```bash
make smoke-observability
```

Override its endpoints with `SMOKE_PROMETHEUS_URL`, `SMOKE_LOKI_URL`, `SMOKE_TEMPO_URL`, and
`SMOKE_OTEL_COLLECTOR_URL`. `TELEMETRY_ENABLED=false` disables OTLP traces/logs while checkout and
the pull-based `/metrics` endpoint remain functional. For host-run services, override
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317`; Compose uses `http://otel-collector:4317`.

## Durable Incident API and queue

Alertmanager posts its bounded v4 payload to `POST /api/v1/alerts`. The API validates required
`alertname` and `service` labels, computes a SHA-256 fingerprint from canonical sorted labels, and
uses a second firing/resolved delivery fingerprint for occurrence idempotency. PostgreSQL unique
constraints are the concurrency boundary. The same firing delivery therefore cannot create a
second incident, occurrence, run, or job.

The ingestion transaction stores canonical state, the normalized occurrence, audit entries, an
`evidence_collection` run, and a `pending_publish` outbox/job row. After commit, the API publishes
a Celery task whose only business argument is the stable incident ID and records `queued` before
returning HTTP 202. A broker error is recorded as `publish_failed` and returned as typed HTTP 503;
an Alertmanager retry republishes the same deterministic task ID safely.

Manual firing example (also present in generated OpenAPI):

```bash
curl --fail-with-body --request POST http://127.0.0.1:8006/api/v1/alerts \
  --header 'Content-Type: application/json' \
  --header 'X-Request-ID: readme-alert-1' \
  --data '{"version":"4","status":"firing","receiver":"incident-api","alerts":[{"status":"firing","labels":{"alertname":"ManualDemoAlert","service":"payment-service","severity":"warning"},"annotations":{"summary":"Manual durable incident example"},"startsAt":"2026-09-03T12:00:00Z","endsAt":"0001-01-01T00:00:00Z","generatorURL":"http://prometheus:9090/graph","fingerprint":"source-owned-value"}]}'
```

Inspect the returned incident, audit history, deterministic run, canonical evidence/timeline, and
operational job state:

```bash
curl --fail-with-body 'http://127.0.0.1:8006/api/v1/incidents?limit=25&offset=0'
curl --fail-with-body http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID
curl --fail-with-body http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID/timeline
curl --fail-with-body http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID/investigation-runs
curl --fail-with-body http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID/evidence
curl --fail-with-body \
  http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID/evidence/timeline
curl --fail-with-body \
  'http://127.0.0.1:8006/api/v1/investigation-jobs?incident_id=REPLACE_WITH_INCIDENT_ID'
curl --fail-with-body \
  'http://127.0.0.1:8006/api/v1/investigation-jobs?status=dead_lettered'
```

The worker loads the canonical incident by ID, moves `queued → investigating`, validates its UTC
window and service scope, then runs repository-owned Prometheus, Loki, Tempo, and deployment
operations concurrently. Each source is persisted independently with `collected`, `empty`,
`unavailable`, `failed`, or `timed_out` status. Completion records run status
`evidence_collected` and audit event `investigation.evidence_collection_completed` with
`ai_executed=false`. It intentionally leaves `root_cause` and `confidence` null and never runs an
LLM. The Celery child owns one process-lifetime telemetry runtime and exposes its bounded adapter
and collection counters/histograms on the loopback-published investigator metrics endpoint.

Register local deployment context through the API; an exact replay is idempotent:

```bash
curl --fail-with-body --request POST http://127.0.0.1:8006/api/v1/deployments \
  --header 'Content-Type: application/json' \
  --data '{"service":"payment-service","environment":"development","version":"0.1.0","deployed_at":"2026-09-03T11:40:00Z","commit_sha":"2222222222222222222222222222222222222222","changed_files":["apps/demo/payment_service/faults.py"],"metadata":{"source":"local-demo"}}'

curl --fail-with-body \
  'http://127.0.0.1:8006/api/v1/deployments?service=payment-service&environment=development'
```

No endpoint or worker surface accepts arbitrary PromQL, LogQL, TraceQL, SQL, shell commands, or
backend URLs. Evidence limits, HTTP deadlines/retries, maximum response bytes, allowed lookback,
future skew, source deadlines, and the separate bounded internal timeline-correlation set are
configured by the `EVIDENCE_*` settings in `.env.example`. Public API pages remain limited to 100.

Application failures retry at 2, then 4 seconds by default (bounded by 30 seconds), up to three
total attempts. The final failure is stored as `dead_lettered`, the run and incident expose failure,
and the Celery task itself fails rather than pretending investigation success. Worker-loss recovery
uses late acknowledgement, one-message prefetch, a 15-second PostgreSQL lease, and a 300-second
Redis visibility timeout. If PostgreSQL is unavailable long enough that failure state cannot be
written, Celery's result backend remains the documented failure record.

Queue behavior is configured by `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND_URL`,
`QUEUE_PUBLISH_TIMEOUT_SECONDS`, `INVESTIGATION_MAX_ATTEMPTS`,
`INVESTIGATION_RETRY_BASE_SECONDS`, `INVESTIGATION_RETRY_MAX_SECONDS`,
`INVESTIGATION_JOB_LEASE_SECONDS`, and `CELERY_VISIBILITY_TIMEOUT_SECONDS`. URL settings use
secret-safe representations. Exact defaults are in `.env.example`.

The old receiver can still be exercised explicitly for its bounded validation contract, but it is
not in the production-shaped route:

```bash
docker compose --profile test-tools up --build --detach alert-receiver
curl --fail-with-body http://127.0.0.1:8005/health/ready
```

## Controlled slow-database scenario

The payment service exposes only the allowlisted local control
`/internal/faults/slow-database`. It always starts disabled. Control is refused unless the process
is in `development` or `test`, `FAULT_INJECTION_ALLOWED=true`, and the
`X-Fault-Control-Token` exactly matches a non-empty configured token. Compose opts in explicitly,
publishes payment on loopback, and uses the documented local placeholder token. The token is
secret-safe in settings and is never printed by the runner.

Run the complete bounded Stage 05 scenario:

```bash
make scenario-incident-pipeline
```

`make scenario-slow-database` remains a compatibility alias. The scenario forces an off baseline,
proves a normal checkout under one second, enables a fixed 2.5-second pre-persistence delay,
generates eight unique checkouts, and polls the real backend APIs for:

- `demo_fault_enabled{service="payment-service",fault="slow_database"} == 1`;
- payment p95 above two seconds from the emitted `/payments` histogram;
- firing `DemoPaymentHighLatency` in Prometheus and active state in Alertmanager;
- a firing webhook persisted as exactly one durable incident;
- a completed queue job and explicitly named `evidence_collection` run;
- newly persisted canonical evidence with at least one `collected` result from each of Prometheus,
  Loki, Tempo, and the local deployment store;
- collected latency, grouped/around logs, slow-trace/dependency, and recent/current-version domain
  templates, while uninstrumented or event-free capabilities remain explicit `empty` results;
- an Incident API evidence timeline in stable UTC order, with every event linked to an evidence ID;
- two repeated webhook deliveries that create no extra occurrence/run/job;
- a Loki fault log and Tempo span carrying `fault.enabled`, service version, and environment;
- then disable, a fast recovery checkout, gauge zero, inactive alert, and a durable resolved update.

The enable block has a `finally` cleanup path. Polling is bounded by 60 seconds per condition and
prints request/trace IDs and diagnostics. Overrides are `SCENARIO_FAULT_CONTROL_TOKEN`,
`SCENARIO_ALERTMANAGER_URL`, `SCENARIO_INCIDENT_API_URL`, `SCENARIO_TRAFFIC_COUNT` (4–12), and
`ENVIRONMENT` (`development` by default).
The matching payment settings are `FAULT_INJECTION_ALLOWED`, `FAULT_CONTROL_TOKEN`, and
`SLOW_DATABASE_DELAY_SECONDS` (2–3).

For a manual state check or explicit disable:

```bash
curl --fail-with-body \
  --header 'X-Fault-Control-Token: local-demo-fault-control' \
  http://127.0.0.1:8004/internal/faults/slow-database

curl --fail-with-body --request PUT \

  --header 'Content-Type: application/json' \
  --header 'X-Fault-Control-Token: local-demo-fault-control' \
  --data '{"enabled":false}' \
  http://127.0.0.1:8004/internal/faults/slow-database
```

## Frontend review UI

Start the stack (`make compose-up`), then open `http://127.0.0.1:5173` (`FRONTEND_PORT`).
The list shows severity, service, status, timestamps, and a confidence/data-gap
indicator per incident. The detail view shows the RCA (or an explicit
insufficient-evidence state), data gaps, recommendations with risk, competing
hypotheses with rejected reasons, evidence cards with provenance and
unavailable-source notices, related knowledge (historical context, never
proof), the correlated timeline, and audit status.

Approve/reject controls appear only for recommendations in
`waiting_for_approval` while the incident is also waiting. Decisions send
`{"incident_version": N, "actor": "..."}` (actor defaults to
`VITE_APPROVAL_ACTOR`, editable inline) with a stable `Idempotency-Key` per
recommendation and decision, so a lost response can be retried safely. A stale
version returns 409 with a refresh prompt; already-decided, not-awaiting, and
wrong-state requests return actionable 409s (`approval_conflict`,
`not_awaiting_approval`, `invalid_state`); replays return the stored decision
with `replayed: true` and a non-duplicating notice. Recommendation cards show
risk, reversibility, rationale evidence IDs, and reviewable parameters.
Approval records the decision and resumes incident state without executing
remediation; rejection moves the incident to `rejected`.

For local frontend development without Compose:

```bash
npm --prefix apps/frontend ci
npm --prefix apps/frontend run dev
```

### Screenshots

Screenshots are not committed; capture them deterministically from a local run:

1. `make compose-up`, wait for healthy services, run `make scenario-incident-pipeline`.
2. Open `http://127.0.0.1:5173` at 1440×900 and capture the incident list.
3. Open a `waiting_for_approval` incident and capture the detail top (RCA +
   recommendations), then the evidence and timeline sections.
4. Approve one recommendation with the default actor and capture the approved
   state; confirm no remediation ran (`docker compose logs investigator-worker`).

## Tests and quality commands

| Command | Purpose |
| --- | --- |
| `make setup` / `make sync` | Synchronize all locked dependency groups. |
| `make format` | Apply Ruff formatting. |
| `make lint` | Run Ruff lint checks. |
| `make typecheck` | Type-check production packages and applications. |
| `make test-unit` | Run isolated unit tests. |
| `make test-contract` | Run demo, receiver-stub, and Incident API contracts. |
| `make test` | Run deterministic tests that need no live services. |
| `make test-integration` | Run clean-migration/store, live checkout, and bounded four-source evidence tests. |
| `make compose-validate` | Validate the resolved Compose model. |
| `make check` | Run the deterministic Stage 05 quality gate (live integration remains separate). |
| `make compose-logs` | Print uncolored service/migration/PostgreSQL logs. |
| `make smoke-observability` | Prove metric, log, and trace correlation through backend APIs. |
| `make scenario-incident-pipeline` | Prove fault→alert→durable incident→worker→recovery. |
| `make eval-fake` | Run the 7-scenario offline eval suite and write `evals/results/`. |
| `make eval-extended` | Run the 12-scenario extended offline eval suite. |
| `make test-eval` | Run eval schema/grader/fault unit tests plus the fake suite. |
| `make eval-live` | Gated bounded live eval (requires explicit flags and cost budget). |
| `make demo-investigation-report` | Print a fixture-based investigator report (no credentials). |
| `make smoke-investigator-live` | Manual live-model smoke; requires explicit opt-in credentials. |
| `make scenario-slow-database` | Compatibility alias for `scenario-incident-pipeline`. |
| `make milestone1-smoke` | Re-run the full checkout, observability, and controlled-fault gate. |
| `make milestone2-smoke` | Run checkout, observability, and durable incident E2E. |
| `make frontend-install` / `frontend-lint` / `frontend-typecheck` / `frontend-test` / `frontend-build` | Node gates for the review UI (install, ESLint, `tsc --noEmit`, Vitest, production build). |
| `make frontend-e2e` | Playwright review-and-approve browser spec (route-fulfilled by default; live with `FRONTEND_E2E_URL` + `FRONTEND_E2E_LIVE=1`). |

Run integration tests only after `docker compose ps` reports the runtime services healthy:

```bash
make test-integration
```

If Compose credentials or host ports are overridden, pass matching `TEST_GATEWAY_URL`,
`TEST_PAYMENT_URL`, `TEST_PROMETHEUS_URL`, `TEST_LOKI_URL`, `TEST_TEMPO_URL`,
`TEST_ALERTMANAGER_URL`, `TEST_INCIDENT_API_URL`, `TEST_INVESTIGATOR_METRICS_URL`,
`TEST_DATABASE_URL`, `LIVE_DATABASE_URL`, and `FAULT_CONTROL_TOKEN` values to Make (or export
them). The target explicitly opts into the bounded fault test with
`RUN_LOCAL_EVIDENCE_INTEGRATION=true`; direct `pytest` runs skip it unless the same flag is present.
Defaults are listed in `.env.example`.

The direct full-gate commands are:

```bash
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy apps packages
uv run pytest -q
docker compose config --quiet
```

Without the environment variables declared by `make test-integration`, live integration tests are
reported as skipped rather than silently connecting to an undeclared database.

## Repository layout

```text
apps/demo/                  # four business services plus optional receiver test tool
apps/frontend/              # React + TypeScript + Vite review UI (list/detail/approval)
apps/incident_api/          # durable Alertmanager ingestion and typed reads
apps/investigator_worker/   # Celery deterministic evidence collection entrypoint
packages/config/            # shared typed settings
packages/evals/             # versioned scenario schema, grader, fixtures, runners
packages/incidents/         # normalization, scoping, collection, timeline, worker service
packages/models/            # strict transport/domain contracts
packages/persistence/       # SQLAlchemy payment/incident/evidence/deployment stores
packages/task_queue/        # Celery configuration, publisher, Redis readiness
packages/telemetry/         # OTel lifecycle, W3C context, JSON logs, and RED metrics
migrations/                 # Alembic environment and versioned schema
infrastructure/docker/      # reusable application image and PostgreSQL init asset
observability/              # Collector, Prometheus/rules, Alertmanager, Loki, Tempo, Grafana
evals/scenarios/            # versioned machine-readable fault scenarios (v1 + v1-extended)
evals/datasets/             # dataset manifests and human explanations
evals/results/              # generated JSON/Markdown reports (not hand-edited)
scripts/                    # bounded checkout, telemetry, and controlled-fault scenarios
tests/unit/                 # deterministic isolated behavior
tests/contract/             # in-process API contracts with fakes
tests/eval/                 # deterministic fake-provider eval suite (CI)
tests/integration/          # declared PostgreSQL and live Compose checks
docs/                       # requirements, architecture, quality gates, and ADRs
```

Application entry points depend on package contracts; reusable packages do not import application
modules. The layout baseline is recorded in [ADR 0001](docs/adr/0001-monorepo-and-technology-baseline.md),
and the checkout/idempotency boundary is recorded in
[ADR 0002](docs/adr/0002-demo-checkout-and-idempotency-boundary.md). The observability and guarded
fault/alert decisions are [ADR 0003](docs/adr/0003-local-observability-and-correlation-baseline.md)
and [ADR 0004](docs/adr/0004-controlled-fault-and-alert-boundary.md).

The durable ingestion/queue boundary is recorded in
[ADR 0005](docs/adr/0005-durable-incident-ingestion-and-queue-boundary.md).

The deterministic collection boundary is recorded in
[ADR 0006](docs/adr/0006-deterministic-evidence-collection-boundary.md). The evidence-grounded
investigator boundary is recorded in
[ADR 0007](docs/adr/0007-evidence-grounded-investigator-boundary.md). Knowledge retrieval is
recorded in [ADR 0008](docs/adr/0008-knowledge-rag-boundary.md) and the frontend/approval
boundary in [ADR 0009](docs/adr/0009-frontend-approval-boundary.md). Eval scenarios and
grading are [ADR 0010](docs/adr/0010-eval-scenarios-and-grading-boundary.md). The next planned
milestone is **implementation-plan Stage 9 / safe remediation and recovery**; Kubernetes,
Terraform, and cloud deployment are deliberately not started here.
