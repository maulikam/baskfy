# Decile — developer entrypoints. See CLAUDE.md.
COMPOSE := docker compose -f infra/docker/compose.yml
UV      := uv run

.DEFAULT_GOAL := help
.PHONY: help up down migrate downgrade seed test test-db e2e lint fmt typecheck schema openapi client doctor fixtures mailpit api web web-build worker beat flower pipeline backfill refdata explain bench loadtest plans bundle

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:            ## Start postgres + redis + mailpit and wait for health
	$(COMPOSE) up -d --wait

down:          ## Stop the local stack (keeps volumes)
	$(COMPOSE) down

migrate:       ## Apply all Alembic migrations to DECILE_DATABASE_URL
	cd services/api && $(UV) alembic upgrade head

downgrade:     ## Roll every migration back to base
	cd services/api && $(UV) alembic downgrade base

seed:          ## Seed reference data (exchange, universes, plans, example screens, trading days)
	$(UV) python -m decile_api.seed all

test:          ## Run the Python and TypeScript test suites
	$(UV) pytest
	pnpm -r run test

test-db:       ## Run only the tests that need a live database
	$(UV) pytest -m db

lint:          ## ruff check + format check + mypy strict + TS lint/typecheck (every package)
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy
	pnpm -r run lint

fmt:           ## Autoformat Python and TS
	$(UV) ruff format .
	$(UV) ruff check --fix .

typecheck:     ## mypy only
	$(UV) mypy

mailpit:       ## Open the local inbox (Prompt 12 §3) — everything the API sent, in a browser
	@echo "http://localhost:$${DECILE_MAILPIT_UI_PORT:-8025}"

doctor:        ## Report what each provider can currently serve
	$(UV) python -m decile_providers.cli doctor

fixtures:      ## Regenerate tests/fixtures/providers from the docs/13 reference export
	$(UV) python -m decile_providers.fixture_builder

schema:        ## Regenerate the ScreenDefinition JSON Schema consumed by packages/api-client
	$(UV) python -m decile_core.screen_definition_schema > packages/api-client/src/screen-definition.schema.json

openapi:       ## Emit packages/api-client/openapi.json from the FastAPI app (docs/07)
	$(UV) python -m decile_api.openapi

client:        ## Regenerate the TypeScript client from openapi.json
	$(MAKE) openapi
	pnpm --filter @decile/api-client run generate

api:           ## Run the FastAPI service on :8000 with reload
	$(UV) uvicorn decile_api.app:get_app --factory --reload --port 8000

web:           ## Run the Next.js dev server on :3000
	pnpm --filter @decile/web run dev

web-build:     ## Production build of the web app
	pnpm --filter @decile/web run build

e2e:           ## Playwright acceptance checks (builds and starts the app itself)
	pnpm --filter @decile/web run e2e

bench:         ## Measure every docs/11 performance budget and write benchmarks/AS-MEASURED.md
	$(UV) pytest -m benchmark
	$(UV) python -m benchmarks.report --check

loadtest:      ## 50 concurrent screen runs against a running API: make loadtest URL=http://localhost:8000
	$(UV) python -m benchmarks.load_screens --base-url $(or $(URL),http://localhost:8000)

plans:         ## EXPLAIN ANALYZE every hot query; add WRITE=1 to re-record the CI baseline
	$(UV) python -m decile_api.query_plans $(if $(WRITE),--write,)

bundle:        ## Client-JS budget for the screens route (needs `make web-build` first)
	cd apps/web && node scripts/bundle-budget.mjs --check

worker:        ## Run a Celery worker across all queues
	$(UV) celery -A decile_worker.celery_app:app worker -Q ingest,compute,backtest,default -l info

beat:          ## Run Celery Beat (docs/09 §Schedule, IST)
	$(UV) celery -A decile_worker.celery_app:app beat -l info

flower:        ## Inspect queues and tasks (docs/02)
	$(UV) celery -A decile_worker.celery_app:app flower

pipeline:      ## Run the nightly pipeline for one date: make pipeline DATE=2026-08-18
	$(UV) python -m decile_worker.pipeline_cli --date $(DATE)

backfill:      ## Resumable bar backfill: make backfill FROM=2011-01-01 TO=2026-08-18
	$(UV) python -m decile_worker.backfill --from $(FROM) --to $(TO) --resume

refdata:       ## Reference-data backfill: make refdata FROM=2018-01-01 TO=2026-08-18
	$(UV) python -m decile_worker.reference_backfill --from $(FROM) --to $(TO)

explain:       ## Audit one factor: make explain SYMBOL=CUPID DATE=2026-08-18 FACTOR=sharpe_12m
	$(UV) python -m decile_worker.factors_cli explain --symbol $(SYMBOL) --date $(DATE) --factor $(FACTOR)
