# Applications

`demo/` contains the four runnable checkout boundaries—gateway, order, inventory, and payment—plus
the Stage 03 disposable Alertmanager receiver test tool. Each module exposes a side-effect-free
`create_app` factory; external clients/stores are injectable for deterministic tests. Payment owns
the single guarded `slow_database` fault; the receiver runs only in the Compose `test-tools`
profile.

`incident_api/` owns durable typed ingestion/reads and `investigator_worker/` owns the Celery
`no_ai_placeholder` task. Both depend on package interfaces and PostgreSQL canonical state. The
frontend is still deferred. See
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for implemented and target boundaries.
