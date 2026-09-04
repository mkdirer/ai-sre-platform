# ADR 0001: Python monorepo and technology baseline

- Status: Accepted
- Date: 2026-09-02
- Scope: Stage 00 repository bootstrap

## Context

The platform will eventually contain several small applications plus shared incident, evidence, persistence, telemetry, and AI capabilities. The local demo must remain reproducible, while the HTTP API and asynchronous investigator need clear runtime boundaries. Stage 00 must establish those boundaries without presenting future applications or infrastructure as implemented.

The repository requirements mandate Python 3.12 and `uv`. The target architecture also establishes FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, Redis/Celery, LangGraph, and the OpenAI Responses API, introduced according to the implementation plan. Docker Compose is the first local orchestration mechanism; Kubernetes and cloud resources come later.

## Decision

### One root Python project

Use one installable root project, one root `pyproject.toml`, and one committed `uv.lock` for the Python monorepo. Hatchling is the build backend. `uv` owns interpreter selection, the development environment, dependency resolution, and command execution. Direct dependencies are declared centrally and exact transitive versions are made reproducible by the lockfile.

Development tooling is also centralized: Ruff formats and lints, mypy checks production packages, and pytest with pytest-asyncio runs deterministic tests. A root Makefile gives contributors short, stable entry points while the underlying `uv` commands remain visible and usable directly.

### Source layout and import direction

Top-level Python package directories mirror the architecture:

```text
apps/
  demo/
    gateway/
    order_service/
    inventory_service/
    payment_service/
  incident_api/
  investigator_worker/
packages/
  agents/
  tools/
  rag/
  models/
  persistence/
  telemetry/
  config/
tests/
  unit/
  integration/
  agent/
  e2e/
```

`apps` contains future deployable entry points and composition code. `packages` contains reusable implementation with explicit interfaces. The allowed dependency direction is:

```text
apps → packages
```

Shared packages do not import from applications. Package-to-package dependencies must remain explicit and acyclic rather than being routed through a generic service locator. External clients will be injected behind typed interfaces when their stages are implemented, allowing deterministic fakes in tests.

All Stage 00 directories that participate in Python imports contain `__init__.py`. Their presence establishes ownership only; it does not imply an API server, worker, telemetry adapter, model workflow, or business behavior exists.

### Shared configuration

Place shared settings in `packages/config/settings.py` and expose their public interface from `packages.config`. Configuration uses Pydantic Settings so values are validated and can be supplied by the process environment or an optional local `.env` file.

Defaults are safe for import and local tests: loading configuration performs no network calls and starts no resources. Fields that can contain credentials use secret-aware values, whose ordinary string and representation forms are redacted. The `.env.example` file contains documentation-safe values only; real `.env` files remain ignored.

### Dependency baseline

Stage 00 installs the pinned runtime libraries needed to begin the next backend stages, but installing a library is not evidence that its component is implemented:

- FastAPI and Pydantic v2 provide typed HTTP boundaries and schemas;
- Pydantic Settings provides validated environment-backed configuration;
- SQLAlchemy 2 async, asyncpg, and Alembic prepare for durable PostgreSQL storage and migrations;
- HTTPX provides asynchronous, timeout-capable HTTP clients;
- Uvicorn will host later ASGI application entry points.

Celery/Redis, LangGraph, the OpenAI integration, pgvector, observability systems, and frontend dependencies are added only in their assigned milestones. Third-party APIs must be implemented against the versions resolved in `uv.lock`, not from memory.

### Compose and continuous integration

Keep a valid root `docker-compose.yml` with no services during Stage 00. `docker compose config --quiet` verifies syntax and preserves the planned local entry point, while build, start, health, and integration checks are not applicable because there is nothing to run. Adding fake or idle containers would create a misleading success signal.

The initial GitHub Actions workflow runs only the reproducible Stage 00 quality gate. It must evolve when real runtime, integration, or frontend components are introduced; empty steps do not stand in for future verification.

## Consequences

Benefits:

- a clean clone resolves one reproducible Python environment;
- application ownership and reusable-library ownership are visible in the filesystem;
- shared settings and test tooling behave consistently across future applications;
- the initial local and CI gates test real imports and configuration without external services;
- an empty Compose model communicates the current state honestly.

Tradeoffs:

- all Python applications currently upgrade dependencies together through one lockfile;
- generic top-level names such as `apps` and `packages` require disciplined absolute imports;
- the root project may eventually need workspace members or separately built distributions if release cadence or deployment isolation demands them;
- Compose build/start and integration gates remain intentionally unavailable until services exist.

## Deferred decisions

This ADR does not select or implement business endpoints, database schemas, migrations, queue messages, observability configuration, AI workflows, evidence tools, RAG storage, a frontend, remediation, Kubernetes, Helm, Terraform, or cloud delivery.

Those decisions belong to their numbered stages. A later change to the import direction, package ownership, queue baseline, or deployment boundary requires a superseding ADR rather than an implicit structural rewrite.
