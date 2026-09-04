# Database migrations

Alembic is the only production schema-creation path. Revision `20260902_0001` creates the Stage 01
payment/order result; `20260902_0002` adds incidents, normalized occurrences, no-AI investigation
runs, audit events, and durable queue tracking. Run **uv run alembic upgrade head** with PostgreSQL
settings supplied through the environment; application startup never calls ORM create-all helpers.
