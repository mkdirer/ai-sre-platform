UV ?= uv
UV_SYNC_FLAGS ?= --all-groups --locked
COMPOSE ?= docker compose
TEST_GATEWAY_URL ?= http://127.0.0.1:8001
TEST_PAYMENT_URL ?= http://127.0.0.1:8004
TEST_DATABASE_URL ?= postgresql+asyncpg://aisre:change-me@127.0.0.1:5432/aisre_test
LIVE_DATABASE_URL ?= postgresql+asyncpg://aisre:change-me@127.0.0.1:5432/aisre

.PHONY: setup sync format format-check lint typecheck test-unit test-contract test \
	test-integration migrate compose-up compose-down compose-logs smoke compose-validate check

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
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" \
	LIVE_DATABASE_URL="$(LIVE_DATABASE_URL)" \
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

compose-validate:
	$(COMPOSE) config --quiet

check: sync lint format-check typecheck test compose-validate
