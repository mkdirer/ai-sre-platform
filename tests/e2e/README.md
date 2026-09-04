# End-to-end tests

`scripts/scenario_slow_database.py` is the bounded Stage 04 E2E runner. It proves controlled fault,
cross-signal telemetry, Prometheus/Alertmanager, one durable incident, duplicate no-op, completed
`no_ai_placeholder` Celery work, cleanup, and resolved ingestion. `make
scenario-incident-pipeline` runs it against healthy Compose dependencies.
