UV ?= uv
UV_SYNC_FLAGS ?= --all-groups --locked
COMPOSE ?= docker compose
TEST_GATEWAY_URL ?= http://127.0.0.1:8001
TEST_PAYMENT_URL ?= http://127.0.0.1:8004
TEST_PROMETHEUS_URL ?= http://127.0.0.1:9090
TEST_LOKI_URL ?= http://127.0.0.1:3100
TEST_TEMPO_URL ?= http://127.0.0.1:3200
TEST_ALERTMANAGER_URL ?= http://127.0.0.1:9093
TEST_INCIDENT_API_URL ?= http://127.0.0.1:8006
TEST_INVESTIGATOR_METRICS_URL ?= http://127.0.0.1:9464/metrics
TEST_DATABASE_URL ?= postgresql+asyncpg://aisre:change-me@127.0.0.1:5432/aisre_test
LIVE_DATABASE_URL ?= postgresql+asyncpg://aisre:change-me@127.0.0.1:5432/aisre
FAULT_CONTROL_TOKEN ?= local-demo-fault-control

.PHONY: setup sync format format-check lint typecheck test-unit test-contract test \
	test-integration test-eval migrate compose-up compose-down compose-logs smoke smoke-observability \
	scenario-slow-database scenario-incident-pipeline scenario-remediation milestone1-smoke milestone2-smoke \
	demo-investigation-report smoke-investigator-live ingest-knowledge ingest-knowledge-dry-run \
	eval-fake eval-extended eval-live \
	frontend-install frontend-lint frontend-typecheck frontend-test frontend-build frontend-e2e \
	compose-validate check

setup: sync

sync:
	$(UV) sync $(UV_SYNC_FLAGS)

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy apps packages

test-unit:
	$(UV) run pytest -q tests/unit

test-contract:
	$(UV) run pytest -q tests/contract

test:
	$(UV) run pytest -q -m "not integration"

test-integration:
	TEST_GATEWAY_URL="$(TEST_GATEWAY_URL)" \
	TEST_PAYMENT_URL="$(TEST_PAYMENT_URL)" \
	TEST_PROMETHEUS_URL="$(TEST_PROMETHEUS_URL)" \
	TEST_LOKI_URL="$(TEST_LOKI_URL)" \
	TEST_TEMPO_URL="$(TEST_TEMPO_URL)" \
	TEST_ALERTMANAGER_URL="$(TEST_ALERTMANAGER_URL)" \
	TEST_INCIDENT_API_URL="$(TEST_INCIDENT_API_URL)" \
	TEST_INVESTIGATOR_METRICS_URL="$(TEST_INVESTIGATOR_METRICS_URL)" \
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" \
	LIVE_DATABASE_URL="$(LIVE_DATABASE_URL)" \
	FAULT_CONTROL_TOKEN="$(FAULT_CONTROL_TOKEN)" \
	RUN_LOCAL_EVIDENCE_INTEGRATION="true" \
	$(UV) run pytest -q -m integration

migrate:
	$(COMPOSE) run --rm --build migrate

compose-up:
	$(COMPOSE) up --build -d

compose-down:
	$(COMPOSE) down

compose-logs:
	$(COMPOSE) logs --no-color

smoke:
	$(UV) run python scripts/smoke_checkout.py

smoke-observability:
	$(UV) run python scripts/smoke_observability.py

scenario-incident-pipeline:
	$(UV) run python scripts/scenario_slow_database.py

scenario-remediation:
	$(UV) run python scripts/scenario_remediation.py

scenario-slow-database: scenario-incident-pipeline

milestone1-smoke: smoke smoke-observability scenario-incident-pipeline

milestone2-smoke: smoke smoke-observability scenario-incident-pipeline

demo-investigation-report:
	$(UV) run python scripts/demo_investigation_report.py

smoke-investigator-live:
	$(UV) run python scripts/smoke_investigator_live.py

ingest-knowledge:
	$(UV) run python scripts/ingest_knowledge.py

ingest-knowledge-dry-run:
	$(UV) run python scripts/ingest_knowledge.py --dry-run

eval-fake:
	$(UV) run python scripts/run_evals.py --dataset v1 --output evals/results

eval-extended:
	$(UV) run python scripts/run_evals.py --dataset v1-extended --output evals/results

eval-live:
	@if [ -z "$${EVAL_MAX_COST_USD}" ] || [ "$${EVAL_MAX_COST_USD}" = "0" ] || [ "$${EVAL_MAX_COST_USD}" = "0.0" ]; then echo "eval-live requires an explicit budget: EVAL_MAX_COST_USD=<usd> make eval-live" >&2; exit 2; fi
	RUN_LIVE_EVALS=1 EVAL_LIVE_CONFIRM=1 $(UV) run python scripts/run_evals.py --mode live --dataset v1-extended --output evals/results

test-eval:
	$(UV) run pytest -q tests/eval tests/unit/test_eval_schema.py tests/unit/test_eval_grader.py tests/unit/test_faults_extended.py

frontend-install:
	npm --prefix apps/frontend ci

frontend-lint:
	npm --prefix apps/frontend run lint

frontend-typecheck:
	npm --prefix apps/frontend run typecheck

frontend-test:
	npm --prefix apps/frontend run test -- --run

frontend-build:
	npm --prefix apps/frontend run build

frontend-e2e:
	npm --prefix apps/frontend run test:e2e

compose-validate:
	$(COMPOSE) config --quiet

check: sync lint format-check typecheck test compose-validate
