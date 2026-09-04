# Stage 03 — controlled fault and alert pipeline

Implement only Milestone 1C. First verify that the normal checkout and cross-signal telemetry from Stages 01–02 still work.

Add the first controlled fault and alert path:

- `slow_database` fault in payment service, disabled by default;
- explicit local-only control that can enable/disable the fault safely and reports current fault state;
- injected DB-related delay around 2–3 seconds while normal behavior remains fast;
- Prometheus alert based on the real repository metric/labels, with a demo-friendly duration and clear annotations;
- Alertmanager configuration and a small deterministic webhook receiver/stub for this stage;
- scripts or Make targets to start the scenario, generate bounded traffic, wait for the alert, verify webhook delivery, disable the fault, and verify recovery;
- visible fault state and deployment/service metadata in logs/traces.

Fault injection must be explicit, reversible, test-environment guarded, thread/concurrency safe, and never accidentally enabled by a missing/invalid environment variable. Do not introduce random failure timing.

Add tests for fault configuration, control authorization/environment guard, metric change, alert rule validation where feasible, and scenario cleanup. Update README and run the complete Milestone 1 gate in `docs/QUALITY_GATES.md` from a clean start.

Do not implement the real Incident API, queue, AI, RAG, frontend, or remediation. Do not commit or start Stage 04. If any Milestone 1 criterion cannot be proven, treat the stage as incomplete and explain the blocker.
