# Implementation plan

## Delivery strategy

Implement one numbered prompt at a time. Each stage ends with a working vertical slice, tests, updated documentation, and a Git checkpoint. Do not start AI/RAG until Milestone 1 is fully accepted.

## Stage 0 — repository bootstrap

Goal: reproducible repository skeleton and developer workflow.

Deliverables:

- Python/uv setup and lockfile;
- package/import structure;
- baseline Makefile, `.gitignore`, `.env.example`;
- Docker Compose skeleton;
- lint/type/test configuration;
- CI smoke workflow;
- README quickstart and ADR directory.

No business features.

## Milestone 1A — demo request flow

Goal: a real checkout passes through all four services and PostgreSQL.

Acceptance:

- one command starts dependencies/services;
- `POST /checkout` completes;
- inventory and payment calls are real HTTP hops;
- persistence and error mapping work;
- contract, unit, and integration tests pass;
- live/ready endpoints distinguish process health from dependency readiness.

## Milestone 1B — observability

Goal: one checkout can be followed across metric, log, and trace systems.

Acceptance:

- all services emit request metrics;
- structured logs include trace/span IDs;
- Tempo shows a complete distributed trace;
- Loki finds the request logs by trace ID;
- Grafana dashboards expose latency, traffic, errors, and saturation;
- service name/version/environment attributes are consistent.

## Milestone 1C — fault and alert path

Goal: controlled `slow_database` produces an alert webhook.

Acceptance:

- fault is disabled by default and reversible;
- latency rises from normal to approximately 2–3 seconds;
- Prometheus detects the threshold violation;
- Alertmanager sends a webhook to a temporary receiver or validated stub;
- an automated smoke scenario demonstrates the path.

Milestone 1 is complete only after 1A–1C pass together.

## Stage 2 — Incident API and queue

Goal: an alert creates a durable, deduplicated incident and asynchronous job.

Status: implemented in repository Stage 04. Repository Stage 05 replaces the active placeholder
with deterministic evidence collection while retaining the historical task/state compatibility
needed for already-published jobs.

Acceptance:

- Alembic migrations create the schema;
- ingestion returns 202 without waiting for investigation;
- duplicate alert delivery is idempotent;
- Celery/Redis job is queued and observable;
- API endpoints and typed errors are documented/tested.

## Stage 3 — evidence adapters

Goal: an incident produces normalized evidence without an LLM.

Status: implemented in repository Stage 05. The live `slow_database` integration exercises the
production alert, queue, worker, all four local sources, PostgreSQL persistence, Incident API reads,
deterministic timeline, and recovery. No AI, LangGraph, RAG, frontend, or remediation is included.

Acceptance:

- Prometheus, Loki, Tempo, and deployment adapters work;
- only allowlisted domain queries are exposed;
- timeouts, result limits, retries, and partial failures are tested;
- evidence provenance and stable IDs are persisted;
- a deterministic correlation/timeline service works.

## Stage 4 — LangGraph baseline

Goal: collected evidence becomes a schema-valid baseline report.

Status: implemented in repository Stage 06. The checkpointed LangGraph workflow
(scope → collect/load → correlate → hypotheses → verification → bounded extra
evidence → sufficiency → report) runs behind `INVESTIGATOR_ENABLED=false` by
default, uses OpenAI Structured Outputs with config-routed models, and persists
run state, hypotheses, reports, call metadata, and failures. Tests use fake
providers; a credential-guarded live smoke is manual-only.

Acceptance:

- graph state and nodes are typed;
- execution is checkpointed and retry-safe;
- real provider is configurable; tests use fake outputs;
- OpenAI Responses API Structured Outputs validate the report;
- every claim references existing evidence;
- insufficient evidence yields null root cause.

## Stage 5 — competing hypotheses and verification

Goal: evidence-first multi-hypothesis investigation with a bounded loop.

Status: implemented in repository Stage 06 together with Stage 4. Deterministic
validation enforces distinct competing categories, support/contradiction
linkage, contradiction rejection, low-confidence caps, iteration/tool budgets,
and eligible-only RCA selection.

Acceptance:

- at least three relevant candidate hypotheses when evidence permits;
- support and contradiction are tracked;
- verification requests domain-specific evidence;
- rejected hypotheses explain why;
- iteration/tool-call budgets prevent loops;
- final RCA is selected only after validation.

## Stage 6 — RAG

Goal: use runbooks and prior incidents as supporting context.

Status: implemented in repository Stage 07. Versioned Markdown knowledge is
chunked, hashed, embedded (fake offline, OpenAI opt-in), stored in pgvector
with a cosine HNSW index, retrieved through allowlisted runbook/prior-incident/
architecture methods after telemetry correlation, delimited as untrusted
context, cited with `KNW-` IDs distinct from current `EVD-` evidence, and never
permitted to override current telemetry. Seeds include a DB-pool incident, an
unrelated CPU incident, a runbook, architecture, and an adversarial probe.

Acceptance:

- pgvector migration and HNSW index;
- deterministic ingestion with hashing/versioning;
- chunking defaults around 500–800 tokens with overlap around 100;
- metadata filters and top-k retrieval;
- citations link to document/chunk IDs;
- prompt-injection content is treated as data;
- current telemetry remains stronger than historical similarity.

## Stage 7 — frontend and human approval

Goal: two focused screens and a durable approval pause.

Status: implemented in repository Stage 08. The React + TypeScript + Vite app
serves incident list and detail views from Compose; the Incident API exposes
report/hypothesis/recommendation/knowledge-chunk reads and idempotent
approve/reject decisions (version-checked, audited, replay-safe) recorded in a
new `approvals` table. Approval resumes durable state without executing
remediation; the LangGraph is not re-entered.

Acceptance:

- incident list and details screens;
- timeline, evidence, hypotheses, confidence, gaps, recommendation;
- approve/reject is concurrency-safe and audited;
- LangGraph pauses/resumes correctly;
- no mutation happens before approval.

## Stage 8 — fault scenarios and evals

Goal: reproducible quality measurement.

Status: implemented in repository Stage 09. Six bounded simulated faults plus
healthy extend `slow_database` (pool exhaustion, bad deployment, inventory
timeout, simulated CPU pressure, deterministic error rate) behind the same
guarded control; twelve versioned JSON scenarios (`v1` core seven plus
`v1-extended` edge five: missing-source, noisy, unrelated-deployment,
ambiguous, prompt-injection; three null-answer cases) load without code
changes; `packages/evals/` grades normalized RCA, service, grounding,
sufficiency, null behavior, recommendation safety, and budgets; the fake
suite runs offline in CI and the live runner is gated by explicit flags and
a cost budget with guaranteed cleanup.

Acceptance:

- at least seven core fault/healthy scenarios initially;
- evaluation works with fake and optionally live model provider;
- metrics from `docs/EVALS.md` are reported;
- failures preserve artifacts for diagnosis;
- grow toward at least 20 varied scenarios after the framework is stable.

## Stage 9 — safe remediation and recovery

Goal: approved local rollback followed by deterministic verification.

Status: implemented in repository Stage 10. A closed action registry allows
only `rollback_payment_deployment` on `payment-service` (typed parameters
validated separately from execution); the Incident API claims one execution
row per approved recommendation (version-checked, replay-safe, concurrency
serialized) and enqueues a worker task; the allowlisted adapter disables the
listed faults with read-back confirmation (unknown outcomes never assumed);
deterministic p95 thresholds over a bounded window verify recovery, with
outages and ambiguity recorded as gaps that never resolve; a manual stop
path ends execution unresolved. The LangGraph is not re-entered.

Acceptance:

- allowlisted action schema;
- stale approval/version checks;
- auditable execution;
- recovery thresholds use telemetry;
- failed recovery does not mark the incident resolved;
- no arbitrary model-generated commands.

## Stage 10 — Kubernetes, Helm, Terraform, CI/CD

Goal: deployment assets that do not compromise the local demo.

Status: implemented in repository Stage 11. The Helm chart
(`infrastructure/helm/ai-sre-platform/`) deploys every demo component
with probes, resources, security contexts, HPA for stateless frontends,
and a migration hook Job, validated by lint/render/kubeconform
(`make k8s-validate`); Terraform modules (network, GKE, Cloud SQL with
pgvector, Artifact Registry, Secret Manager, service accounts) validate
with fmt/validate only and never apply; GitHub Actions separate planning
(`platform.yml`: Helm/Terraform/scans) from dev delivery (`deploy.yml`:
versioned build, SBOM/provenance, WIF push, protected-environment deploy,
post-deploy smoke) with manual budget-gated live evals (`eval-live.yml`).

Order:

1. Kubernetes on a local cluster;
2. Helm chart and values validation;
3. Terraform modules for GKE, Cloud SQL PostgreSQL, Artifact Registry, Secret Manager, and supporting network/storage;
4. GitHub Actions quality/build/security/deploy pipeline;
5. post-deploy E2E incident test.

Cloud apply/deploy is never automatic from a development prompt. Planning and validation are separate from creating billable resources.

## Stage 11 — final hardening

Goal: reliable 3–5 minute portfolio demo.

Acceptance:

- clean setup is documented and timed;
- `make demo` starts the local platform;
- one scenario command tells the full story;
- screenshot-ready dashboards and UI;
- architecture/security/eval tradeoffs documented;
- complete review finds no critical issues;
- claims in README are backed by measured results.
