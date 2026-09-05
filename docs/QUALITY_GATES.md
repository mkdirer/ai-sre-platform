# Quality gates

## Per-task gate

Before finishing any prompt:

1. Inspect `git diff --check`.
2. Run format/lint on changed languages.
3. Run static typing for changed production modules.
4. Run focused unit tests.
5. Run relevant integration tests when dependencies are available.
6. Update docs/contracts for changed behavior.
7. Report skipped checks and why.

## Full Python gate

```bash
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy apps packages
uv run pytest -q
```

Coverage targets should be applied to important domain/agent logic, not gamed globally. Critical evidence validation, alert deduplication, status transitions, and approval logic require branch coverage.

## Infrastructure gate

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Then run documented health and smoke checks. Capture container logs when a health check fails. Shut down with volumes preserved unless the test explicitly requires clean data.

## Frontend gate

```bash
npm --prefix apps/frontend ci
npm --prefix apps/frontend run lint
npm --prefix apps/frontend run typecheck
npm --prefix apps/frontend run test -- --run
npm --prefix apps/frontend run build
```

## Migration gate

For every database schema change:

1. Apply migrations to an empty database.
2. Apply from the previous committed revision.
3. Check the current revision is unique.
4. Test required constraints and indexes.
5. Downgrade only when the project explicitly supports it; otherwise document forward-only behavior.

## Milestone 1 end-to-end gate

From a clean local state:

1. Start the stack.
2. Wait for readiness.
3. Submit a normal checkout and verify success.
4. Find its trace in Tempo.
5. Find correlated logs in Loki by trace ID.
6. Verify Prometheus request/latency/error series.
7. Enable `slow_database`.
8. Generate bounded load.
9. Verify elevated latency and firing alert.
10. Verify Alertmanager webhook receipt.
11. Disable the fault and verify recovery.

## Incident platform gate

Stage 2 requires:

- Alert webhook returns 202.
- Duplicate payload does not create a duplicate active incident.
- A queued job survives API restart.
- Status transitions reject invalid movement.

Stage 3 extends this gate with:

- Evidence from each source is persisted with provenance.
- Partial source failure is visible and does not fabricate results.

## AI/agent gate

Use deterministic fixtures. Required cases:

- correct root cause with supporting evidence;
- competing hypothesis rejected by contradiction;
- missing telemetry produces data gap, not negative claim;
- insufficient evidence produces null root cause;
- nonexistent evidence ID causes validation failure;
- malicious log/document text cannot alter workflow/tool rules;
- malformed model response is retried/fails safely;
- loop stops at iteration/tool budget;
- no recommendation mutates state without approval.

## Release gate

- All required checks pass on a clean clone.
- Dependency/container security scan has no untriaged critical finding.
- `.env` and secrets are absent from Git history being delivered.
- License and attribution requirements are satisfied.
- README setup commands were executed exactly as written.
- Demo has been run twice from a clean state.
- Published numeric claims come from stored evaluation output.
