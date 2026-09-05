# AI SRE Platform

A self-contained demo of evidence-grounded incident investigation. A small
checkout system (gateway → order → inventory/payment → PostgreSQL) emits real
metrics, logs, and traces. When something breaks, an API turns the alert into
an investigated incident: telemetry evidence, competing hypotheses, a
human-approved rollback, and verified recovery.

Everything runs locally with Docker Compose. No cloud account and no API keys
needed for the default path.

## Quickstart

You need Python 3.12, [uv](https://docs.astral.sh/uv/) 0.12.9, Docker with the
Compose v2 plugin, and GNU Make.

```bash
make setup            # install locked dependencies
cp .env.example .env  # local-only defaults; .env is git-ignored
make demo             # clean start: rebuild, launch 15 services, wait for health
```

`make demo` wipes local demo data and starts fresh. To keep data between
restarts, use `make compose-up` / `make compose-down` instead.

Then open the UI at http://127.0.0.1:5173 and Grafana at
http://127.0.0.1:3000. The incident list starts empty — that is expected.

## The one-minute incident story

```bash
make scenario-remediation
```

This runs the whole loop in about a minute: a bad payment deployment is
registered, latency degrades, an incident is ingested, evidence is collected
from metrics/logs/traces/deployments, an investigation report is written,
you (via the script) approve the rollback recommendation, the faulty
deployment is rolled back, recovery is verified against live telemetry, and
the incident resolves.

Open the incident in the UI afterwards: you will see the root cause with
confidence, a rejected competing hypothesis with its contradicting evidence,
the evidence cards with provenance, and the approval record.

Two smaller checks if you want them separately:

```bash
make smoke                 # checkout, persistence read, idempotent replay
make smoke-observability   # one checkout proven across metrics, logs, traces
make scenario-incident-pipeline  # controlled slow-database fault to resolved incident
```

## Try the API by hand

Submit a checkout (the `Idempotency-Key` makes retries safe — same key and
body returns the original payment with `idempotent_replay: true`, same key
with a different body returns 409):

```bash
curl --fail-with-body --request POST http://127.0.0.1:8001/checkout \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: hello-1' \
  --data '{"customer_id":"customer-1","sku":"widget-001","quantity":2}'
```

Browse incidents, evidence, hypotheses, and reports:

```bash
curl --fail-with-body 'http://127.0.0.1:8006/api/v1/incidents?limit=5'
curl --fail-with-body http://127.0.0.1:8006/api/v1/incidents/REPLACE_WITH_INCIDENT_ID/report
```

Full endpoint reference with schemas: http://127.0.0.1:8006/docs

## Where to look

| What | Address |
|---|---|
| Review UI (list, detail, approve/reject) | http://127.0.0.1:5173 |
| Grafana RED dashboard | http://127.0.0.1:3000/d/demo-services-red |
| Prometheus / Alertmanager | http://127.0.0.1:9090 / http://127.0.0.1:9093 |
| Service `/metrics` | http://127.0.0.1:8001 … `:8004`, `:8006/metrics`, `:9464/metrics` |
| Logs / traces | Loki `:3100`, Tempo `:3200` (or `make smoke-observability`) |

The browser calls the API under the same origin at `/api/...`, proxied to the
Incident API by nginx. Approval controls appear only for recommendations that
are actually waiting; stale or already-decided requests get a 409 telling you
to refresh.

## Tests

Deterministic tests need no running services:

```bash
make lint && make typecheck && make test
```

With the stack up, run the live contract (migrations, stores, checkout chain,
four-source evidence):

```bash
make test-integration
```

Frontend checks: `npm --prefix apps/frontend ci`, then `run lint`,
`run typecheck`, `run test -- --run`, `run build`.

Offline evals (scripted providers, no cost, reports in `evals/results/`):

```bash
make eval-fake      # 7 core scenarios
make eval-extended  # 12 scenarios incl. edge cases (missing telemetry,
                    # unrelated deploys, prompt injection, healthy nulls)
```

The stored reports show 7/7 and 12/12 with a fake provider — they measure the
grader and fixtures, not a live model. Live-model evals are manual,
confirmation-typed, and budget-capped (`make eval-live` refuses without them).

## How it fits together

- `apps/demo/` — four checkout services plus an alert-receiver test tool.
  Payment owns guarded fault switches (always off, token + dev/test only).
- `apps/incident_api/` — validates Alertmanager webhooks, deduplicates by
  fingerprint, persists incidents, queues Celery jobs carrying only IDs.
- `apps/investigator_worker/` — collects Prometheus/Loki/Tempo/deployment
  evidence concurrently (partial failures are data, not errors) and, when
  enabled, runs a checkpointed LangGraph investigator via OpenAI Structured
  Outputs. Every claim must cite evidence IDs; thin evidence yields
  `root_cause: null` instead of a guess.
- `packages/` — strict Pydantic contracts, SQLAlchemy stores with Alembic
  migrations, bounded telemetry clients, eval schema/grader, closed
  remediation registry (exactly one reversible action: roll back the payment
  deployment, verified by repeated healthy p95 polls).
- `apps/frontend/` — two screens (list, detail) reading only from the API.
- `observability/` — Prometheus rules, Alertmanager routing, Collector, Loki,
  Tempo, and Grafana provisioning mounted into Compose.
- `infrastructure/` — local Kubernetes chart and plan-only Terraform/GCP
  modules plus CI workflows. Validated, never applied automatically.

## Configuration

Copy `.env.example` to `.env` and adjust ports, budgets, or thresholds there;
Compose reads the same file. The checked-in password and fault token are
local-demo placeholders — replace them anywhere outside localhost. Secret
settings use `SecretStr` and logs redact credential shapes.

To start over completely: `docker compose down --volumes --remove-orphans`
and run `make demo` again.

## When something breaks

- **Port already in use** — another stack holds 8001–8006, 9090/9093,
  3000/3100/3200, 5173, 9464, or 5432/6379. Stop it, or override the
  `*_PORT` variable in `.env` (see `.env.example`).
- **Fault refuses to enable** — needs `ENVIRONMENT` development/test,
  `FAULT_INJECTION_ALLOWED=true`, and the exact `X-Fault-Control-Token`.
  Compose sets all three; plain `uv run` does not.
- **Alert never fires** — run `make smoke-observability` first, then check
  Prometheus `:9090` for `DemoPaymentHighLatency` and Alertmanager `:9093`.
  Only `slow_database` has a real alert rule; other faults use the
  remediation scenario's synthetic webhook path.
- **Incident API 503s** — usually Redis down (`docker compose ps redis`);
  Alertmanager retries and the outbox republishes with the same task ID.
- **Approval 409s** — refresh the detail view; `stale_version` means the
  incident moved, replays return the stored decision.

## License

MIT — see [LICENSE](LICENSE).
