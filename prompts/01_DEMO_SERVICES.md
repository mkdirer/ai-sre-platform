# Stage 01 — demo microservices and checkout flow

Implement only Milestone 1A from `docs/IMPLEMENTATION_PLAN.md`.

Read the repository instructions and relevant docs, inspect existing code/diff/tests, and present a concise implementation plan. Build four small FastAPI applications: gateway, order service, inventory service, and payment service, plus PostgreSQL persistence. Keep the business domain deliberately small but real.

Required behavior:

- Gateway exposes `POST /checkout` with typed request/response and correlation/request ID handling.
- The request performs real HTTP calls gateway → order → inventory → payment; payment persists a transaction/order result in PostgreSQL.
- Define explicit timeouts, error mapping, and idempotency for checkout so a safe retry does not double-charge/create duplicate records.
- Use async SQLAlchemy 2 and Alembic migrations. Do not rely on ORM auto-create outside disposable tests.
- Each service exposes `/health/live` and `/health/ready`; readiness checks only its required dependencies.
- Configuration comes from typed settings and environment variables.
- Container images are minimal, run as non-root where practical, and have health checks.
- Docker Compose starts the four services and PostgreSQL with dependency health ordering.
- Add unit tests, service contract tests, migration tests, and an integration happy-path test. External HTTP hops should be replaceable with fakes in unit tests.
- Add a bounded smoke/load script that prints the request/correlation ID and validates the response.
- Update README with exact startup, migration, checkout, test, and shutdown commands.

Do not add OpenTelemetry, Prometheus, Loki, Tempo, Alertmanager, AI, RAG, a queue, frontend, or fault injection yet.

Acceptance evidence must include: successful clean migration, healthy Compose services, one successful checkout crossing the real service chain, persistence verification through application/API behavior, idempotent retry test, and all applicable quality checks. Do not commit or start Stage 02.
