#!/usr/bin/env bash
# Run .github/workflows/ci.yml's steps on this machine (MERGE-PROMPTS.md M5 step 2:
# "Run each workflow step locally exactly as CI would, and record the results").
#
# Steps that need GitHub's service containers -- a Postgres seeded as baskfy_test, a Redis --
# are reported SKIP with the reason rather than being quietly dropped. A local run that prints
# all-green while silently skipping half the workflow is worse than one that says what it did
# not do. `make up` (M7) is what turns most of those skips into runs.
#
# Usage:  tools/ci-local.sh [--verbose]
# Exit:   0 if every step that could run passed; 1 otherwise.

set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
VERBOSE=${1:-}
PASS=0; FAIL=0; SKIP=0
declare -a FAILED=() SKIPPED=()

run() {           # run <job> <name> <dir> <command...>
  local job=$1 name=$2 dir=$3; shift 3
  printf '  %-9s %-52s ' "[$job]" "$name"
  local out; out=$(cd "$dir" && eval "$*" 2>&1)
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "PASS"; PASS=$((PASS+1))
  else
    echo "FAIL (exit $rc)"; FAIL=$((FAIL+1)); FAILED+=("$job/$name")
    [ -n "$VERBOSE" ] && printf '%s\n' "$out" | tail -25 | sed 's/^/            /'
  fi
  return 0
}

skip() {          # skip <job> <name> <reason>
  printf '  %-9s %-52s SKIP — %s\n' "[$1]" "$2" "$3"
  SKIP=$((SKIP+1)); SKIPPED+=("$1/$2 — $3")
}

D=decile-blueprint
K=kite-momentum-rebalancer
have_pg() { docker exec baskfy-postgres pg_isready -U baskfy >/dev/null 2>&1; }

echo "== namespace =="
run namespace "No namespace token survived the rename" . "tools/check-namespace.sh"

echo "== desk =="
run desk "Install pinned dependencies" "$K" "uv pip install --python ./.venv/bin/python -q -r requirements.txt"
run desk "Tests" "$K" "DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false ./.venv/bin/python -m pytest -q"

echo "== python =="
run python "uv sync --frozen" "$D" "uv sync --frozen"
run python "Lint and type-check" "$D" "uv run ruff check . && uv run ruff format --check . && uv run mypy"
if have_pg; then
  run python "Tests, with per-package coverage gates" "$D" "uv run pytest --cov --cov-report=json:coverage.json && uv run python -m tools.coverage_gate --report coverage.json"
  run python "Hot-query plans have not regressed" "$D" "uv run pytest -q services/api/tests/test_query_plans.py"
else
  run python "Tests (no database: db-marked suites skip)" "$D" "uv run pytest"
  skip python "Tests, with per-package coverage gates" "coverage gate needs the db-marked suites; bring up the stack (M7)"
  skip python "Hot-query plans have not regressed" "needs a live PostgreSQL with the schema (M7)"
fi
run python "Reconciliation report against the reference export" "$D" "uv run python -m baskfy_worker.reconcile_cli"
run python "Performance budgets (docs/11)" "$D" "uv run pytest -m benchmark -q"
if docker info >/dev/null 2>&1; then
  run python "Prometheus rules and config are loadable" "$D" \
    "docker run --rm --entrypoint promtool -v \"\$PWD/infra/prometheus:/rules:ro\" prom/prometheus:v3.1.0 check rules /rules/alerts.yml"
else
  skip python "Prometheus rules and config are loadable" "docker daemon not running"
fi

echo "== client =="
run client "pnpm install --frozen-lockfile" "$D" "pnpm install --frozen-lockfile"
run client "openapi.json is current" "$D" "uv run python -m baskfy_api.openapi --check"
run client "Generated client is current" "$D" "pnpm --filter @baskfy/api-client run generate"
run client "Fail if the checked-in client is stale" "$D" "git diff --exit-code -- packages/api-client/src/generated packages/api-client/openapi.json"
run client "Type-check and test the client" "$D" "pnpm --filter @baskfy/api-client run lint && pnpm --filter @baskfy/api-client run test"

echo "== web =="
run web "Lint and type-check" "$D" "pnpm --filter @baskfy/web run lint"
run web "Unit tests" "$D" "pnpm --filter @baskfy/web run test"
skip web "Install Playwright browsers" "browser download; run 'pnpm --filter @baskfy/web exec playwright install chromium' by hand"
skip web "Acceptance checks (e2e)" "needs Playwright browsers, a seeded baskfy_e2e database and both servers (M7)"
skip web "Client-JS budget" "needs a production build first (pnpm --filter @baskfy/web run build)"

echo
echo "-------------------------------------------------------------------"
printf 'passed %d   failed %d   skipped %d\n' "$PASS" "$FAIL" "$SKIP"
[ ${#SKIPPED[@]} -gt 0 ] && { echo; echo "Skipped, and why:"; printf '  - %s\n' "${SKIPPED[@]}"; }
[ ${#FAILED[@]}  -gt 0 ] && { echo; echo "Failed:";           printf '  - %s\n' "${FAILED[@]}"; }
[ "$FAIL" -eq 0 ] || exit 1
