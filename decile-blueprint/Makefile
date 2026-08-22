# Baskfy — developer entrypoints. See CLAUDE.md.
COMPOSE := docker compose -f infra/docker/compose.yml
UV      := uv run

.DEFAULT_GOAL := help
.PHONY: help up down migrate downgrade seed test test-db e2e lint fmt typecheck schema openapi client doctor fixtures mailpit api web web-build worker beat flower pipeline backfill refdata explain bench loadtest plans bundle backup restore integrity drill metrics coverage reconcile mutants token-sync shadow backend-parity friday-drill

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:            ## Start postgres + redis + mailpit and wait for health
	$(COMPOSE) up -d --wait

down:          ## Stop the local stack (keeps volumes)
	$(COMPOSE) down

migrate:       ## Apply all Alembic migrations to BASKFY_DATABASE_URL
	cd services/api && $(UV) alembic upgrade head

downgrade:     ## Roll every migration back to base
	cd services/api && $(UV) alembic downgrade base

seed:          ## Seed reference data (exchange, universes, plans, example screens, trading days)
	$(UV) python -m baskfy_api.seed all

test:          ## Run the Python and TypeScript test suites
	$(UV) pytest
	pnpm -r run test

test-db:       ## Run only the tests that need a live database
	$(UV) pytest -m db

# --- Prompt 19: coverage gates, reconciliation, mutation testing ------------

coverage:      ## Per-package coverage gate (docs: core >= 90%, api >= 80%). Needs `make up`.
	@test -n "$$BASKFY_TEST_DATABASE_URL" || { \
	  echo "BASKFY_TEST_DATABASE_URL is unset — the db-marked tests would skip and the gate"; \
	  echo "would measure something other than what CI measures. Start the stack and export it:"; \
	  echo "  export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test"; \
	  exit 2; }
	$(UV) pytest --cov --cov-report=term-missing --cov-report=json:coverage.json
	$(UV) python -m tools.coverage_gate --report coverage.json

reconcile:     ## Reconcile our formulas against the reference export; rewrites reconciliation/REPORT.md
	$(UV) python -m baskfy_worker.reconcile_cli --write reconciliation/REPORT.md

mutants:       ## Mutation-test baskfy_core.factors and .screener (Prompt 19 §6)
	$(UV) python -m tools.mutation --workers $(or $(WORKERS),4)

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
	@echo "http://localhost:$${BASKFY_MAILPIT_UI_PORT:-8025}"

doctor:        ## Report what each provider can currently serve
	$(UV) python -m baskfy_providers.cli doctor

fixtures:      ## Regenerate tests/fixtures/providers from the docs/13 reference export
	$(UV) python -m baskfy_providers.fixture_builder

schema:        ## Regenerate the ScreenDefinition JSON Schema consumed by packages/api-client
	$(UV) python -m baskfy_core.screen_definition_schema > packages/api-client/src/screen-definition.schema.json

openapi:       ## Emit packages/api-client/openapi.json from the FastAPI app (docs/07)
	$(UV) python -m baskfy_api.openapi

client:        ## Regenerate the TypeScript client from openapi.json
	$(MAKE) openapi
	pnpm --filter @baskfy/api-client run generate

api:           ## Run the FastAPI service on :8000 with reload
	$(UV) uvicorn baskfy_api.app:get_app --factory --reload --port 8000

web:           ## Run the Next.js dev server on :3000
	pnpm --filter @baskfy/web run dev

web-build:     ## Production build of the web app
	pnpm --filter @baskfy/web run build

e2e:           ## Playwright acceptance checks (builds and starts the app itself)
	pnpm --filter @baskfy/web run e2e

bench:         ## Measure every docs/11 performance budget and write benchmarks/AS-MEASURED.md
	$(UV) pytest -m benchmark
	$(UV) python -m benchmarks.report --check

loadtest:      ## 50 concurrent screen runs against a running API: make loadtest URL=http://localhost:8000
	$(UV) python -m benchmarks.load_screens --base-url $(or $(URL),http://localhost:8000)

plans:         ## EXPLAIN ANALYZE every hot query; add WRITE=1 to re-record the CI baseline
	$(UV) python -m baskfy_api.query_plans $(if $(WRITE),--write,)

bundle:        ## Client-JS budget for the screens route (needs `make web-build` first)
	cd apps/web && node scripts/bundle-budget.mjs --check

# --- Prompt 17: observability, admin and operations -------------------------

metrics:       ## Print the Prometheus exposition a scrape would see (needs `make api`)
	@curl -sf http://localhost:8000/metrics || echo "no API on :8000 — run \`make api\`"

backup:        ## pg_dump the local database into .backups/ (no upload)
	infra/backup/pg_backup.sh --no-upload

restore:       ## Restore a dump into a scratch database: make restore DUMP=.backups/x.dump
	infra/backup/restore.sh --dump $(DUMP) --target $(or $(TARGET),postgresql://baskfy:baskfy@localhost:5433/baskfy_restore)

integrity:     ## Assert a database could serve the app: make integrity URL=postgresql+asyncpg://...
	$(UV) python -m baskfy_api.integrity $(if $(URL),--database-url $(URL),)

drill:         ## The monthly restore drill, end to end, against the local stack (docs/11 §Reliability)
	$(MAKE) backup
	infra/backup/restore.sh --dump "$$(cat .backups/LATEST)" --target postgresql://baskfy:baskfy@localhost:5433/baskfy_restore
	$(UV) python -m baskfy_api.integrity --database-url postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_restore

worker:        ## Run a Celery worker across all queues
	$(UV) celery -A baskfy_worker.celery_app:app worker -Q ingest,compute,backtest,default -l info

beat:          ## Run Celery Beat (docs/09 §Schedule, IST)
	$(UV) celery -A baskfy_worker.celery_app:app beat -l info

flower:        ## Inspect queues and tasks (docs/02)
	$(UV) celery -A baskfy_worker.celery_app:app flower

pipeline:      ## Run the nightly pipeline for one date: make pipeline DATE=2026-08-18
	$(UV) python -m baskfy_worker.pipeline_cli --date $(DATE)

backfill:      ## Resumable bar backfill: make backfill FROM=2011-01-01 TO=2026-08-18
	$(UV) python -m baskfy_worker.backfill --from $(FROM) --to $(TO) --resume

refdata:       ## Reference-data backfill: make refdata FROM=2018-01-01 TO=2026-08-18
	$(UV) python -m baskfy_worker.reference_backfill --from $(FROM) --to $(TO)

bhavcopy:      ## Bar backfill from the NSE bhavcopy (no Kite needed): make bhavcopy FROM=2024-01-01 TO=2026-08-18
	$(UV) python -m baskfy_worker.bhavcopy_backfill --from $(FROM) --to $(TO)

explain:       ## Audit one factor: make explain SYMBOL=CUPID DATE=2026-08-18 FACTOR=sharpe_12m
	$(UV) python -m baskfy_worker.factors_cli explain --symbol $(SYMBOL) --date $(DATE) --factor $(FACTOR)

DESK    := ../kite-momentum-rebalancer

token-sync:    ## Pull today's Kite token from the box into the local encrypted store: make token-sync TARGET=desk@1.2.3.4
	@test -n "$(TARGET)" || { echo "TARGET is required, e.g. make token-sync TARGET=desk@1.2.3.4"; exit 2; }
	cd $(DESK) && .venv/bin/python scripts/token_sync.py --target $(TARGET)

shadow:        ## One Friday's plan both ways, diffed at order level: make shadow DATE=2026-08-18
	@test -n "$(DATE)" || { echo "DATE is required, e.g. make shadow DATE=2026-08-18"; exit 2; }
	cd $(DESK) && .venv/bin/python -m scripts.shadow_mode --date $(DATE)

backend-parity:  ## Render every desk page on both backends and diff the HTML (M19 §3)
	cd $(DESK) && .venv/bin/python -m scripts.backend_parity

friday-drill:  ## The full Friday loop under DRY_RUN, zero orders: make friday-drill DATE=2026-08-18
	@test -n "$(DATE)" || { echo "DATE is required, e.g. make friday-drill DATE=2026-08-18"; exit 2; }
	cd $(DESK) && DRY_RUN=true .venv/bin/python -m scripts.friday_drill --date $(DATE)
