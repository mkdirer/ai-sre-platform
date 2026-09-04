# Stage 00 — bootstrap the repository

You are implementing only repository foundations for the AI SRE platform.

First read `AGENTS.md`, `docs/PRODUCT_REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/QUALITY_GATES.md`, and inspect the complete repository and Git status. State a short plan, then implement it. Do not implement any business endpoint, observability stack, AI workflow, RAG, frontend, Kubernetes, or cloud resources in this stage.

Create a clean Python 3.12 monorepo foundation using `uv` with:

- a valid `pyproject.toml` and committed `uv.lock`;
- importable package/application skeleton matching the architecture;
- development dependencies for pytest, pytest-asyncio, ruff, and mypy;
- baseline runtime dependencies needed for the next backend stage, pinned through the lockfile;
- `.gitignore`, `.dockerignore`, `.env.example`, `Makefile`, and a minimal `docker-compose.yml` skeleton that validates but does not pretend services are implemented;
- test directories and one meaningful smoke test for package/import/config loading;
- shared typed settings with safe defaults and secret-safe representation;
- README with prerequisites, setup, commands, architecture link, and current implementation status;
- `docs/adr/0001-monorepo-and-technology-baseline.md` documenting the chosen layout;
- a minimal GitHub Actions quality workflow only if it can run the current smoke gate without fake steps.

Make commands should include at least setup/sync, format, lint, typecheck, unit test, full check, and Compose validation. Avoid placeholder code that falsely looks implemented; use explicit TODO documentation for future components.

Run all currently applicable checks from `docs/QUALITY_GATES.md`, fix failures, and inspect the final diff. Finish with changed files, exact commands/results, assumptions, risks, and the next stage. Do not make a Git commit and do not start Stage 01.
