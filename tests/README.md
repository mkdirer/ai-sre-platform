# Test suites

- `unit/`: package/config, retry/error, telemetry, fault-safety, normalization/fingerprinting,
  transition policy, queue payload, worker retry/dead-letter, alert-rule, and cleanup behavior;
- `contract/`: checkout services, retained receiver test tool, and Incident API with deterministic
  injected fakes;
- `integration/`: empty/prior-revision migrations, concurrent durable incident/store behavior,
  worker idempotency/failure state, and the live Compose checkout chain;
- `agent/`: deterministic investigator suites (workflow, validation, gateway
  budgets, evidence tools, AI worker service) with scripted fake providers;
- `e2e/`: the bounded Stage 04 fault→alert→durable incident→Celery placeholder scenario lives in
  `scripts/scenario_slow_database.py` and polls real backend APIs without fixed sleeps.

The deterministic gate excludes tests marked `integration`. Live tests run only when their URLs
and test/runtime database URLs are explicitly declared; `make test-integration` supplies the
documented local Compose defaults.
