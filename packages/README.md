# Shared packages

Stage 04 uses six reusable packages:

- `config`: environment-backed typed settings with secret-safe representations;
- `models`: strict checkout, health, error, fault, alert, incident, run, and job contracts;
- `incidents`: Alertmanager normalization/fingerprinting, transition policy, and retry-safe
  placeholder worker service;
  boundaries;
- `persistence`: async SQLAlchemy metadata, engine construction, payment store, incident store,
  audit history, and durable queue tracking used by Alembic and applications;
- `task_queue`: JSON-only Celery configuration, incident-ID publisher, and Redis readiness probe;
- `telemetry`: lifecycle-owned OpenTelemetry traces/logs, W3C propagation, JSON enrichment, and
  bounded Prometheus request/fault metrics.

`tools`, `agents`, and `rag` remain empty ownership boundaries for later milestones; importability
does not imply those systems exist. See
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).
