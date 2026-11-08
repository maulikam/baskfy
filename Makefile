.DEFAULT_GOAL := help
TAG ?= local

.PHONY: help dev test test-py test-web lint image up down status verify pipeline clean

help: ## List these targets
	@grep -E '^[a-z -]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 24

dev: ## Debug build of the three binaries
	cargo build -p baskfy-api -p baskfy-worker -p baskfy-desk --bins

test: ## Rust tests, always dry
	cargo nextest run --workspace

test-py: ## Python oracle suites
	cd legacy/python/decile-blueprint && uv run pytest -p no:randomly -q --no-cov
	cd legacy/python/kite-momentum-rebalancer && DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false .venv/bin/python -m pytest -q

test-web: ## Web lint and unit tests
	pnpm --filter @baskfy/web run lint && pnpm --filter @baskfy/web run test

lint: ## fmt, clippy, namespace
	cargo fmt --check && cargo clippy --workspace --all-targets -- -D warnings
	bash tools/check-namespace.sh

image: ## docker build baskfy-rs:TAG
	docker build -f deploy/Dockerfile.rust --build-arg GIT_SHA=$$(git rev-parse HEAD) -t baskfy-rs:$(TAG) .

up down status verify: ## The local stack, via tools/deploy/local.sh
	bash tools/deploy/local.sh $@

pipeline: ## Redeploy worker, ingest-worker, beat at TAG
	bash tools/deploy/local.sh pipeline $(TAG)

clean: ## Remove build output
	cargo clean
	find . -type d \( -name node_modules -o -name .next -o -name __pycache__ \) -prune -not -path './frozen/*' -exec rm -rf {} +
