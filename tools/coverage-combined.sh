#!/usr/bin/env bash
# Measure baskfy_core's coverage across BOTH suites, because both of them test it.
#
# M15-M17 moved the desk's score, basket construction, exposure overlay and cost model into
# packages/core. Their tests did not move: they live in kite-momentum-rebalancer/tests/, which runs
# in a different virtualenv and never reached the coverage report.
#
# So the gate saw exposure/regime.py at 39% and basket.py at 15.9% and failed baskfy_core at
# 85.23%, while the desk's 1,322 tests were exercising exactly that code. Combined, the same code
# is at 95% and 81.6%, and the package is at 93.16% against a 90% gate.
#
# Nothing about the code changed between those two numbers. Only which tests were counted.
#
#   tools/coverage-combined.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DECILE="$ROOT/decile-blueprint"
DESK="$ROOT/kite-momentum-rebalancer"
WORK="${COVERAGE_WORKDIR:-$ROOT/.coverage-combined}"

# Without this the db-marked suites skip and the gate refuses to report a number it does not
# trust -- which is the correct behaviour, and was silently the situation for every local run.
export BASKFY_TEST_DATABASE_URL="${BASKFY_TEST_DATABASE_URL:-postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test}"

rm -rf "$WORK"; mkdir -p "$WORK"

cat > "$WORK/desk-coverage.ini" <<INI
[run]
branch = true
source = baskfy_core
parallel = true
data_file = $WORK/.coverage.desk
INI

echo "==> the screener's suite (benchmarks deselected: a latency budget under a profiler"
echo "    measures the profiler, and they run in their own step)"
( cd "$DECILE" && uv run pytest -m 'not benchmark' --cov --cov-report= -q -p no:randomly )
cp "$DECILE/.coverage" "$WORK/.coverage.from-decile"

echo "==> the desk's suite, measuring the core modules it owns the tests for"
( cd "$DESK" && .venv/bin/python -m pytest tests/ -q \
    --cov --cov-config="$WORK/desk-coverage.ini" --cov-report= )

echo "==> combining"
( cd "$DECILE" && uv run python -m coverage combine --keep \
    --data-file="$WORK/.coverage" "$WORK/.coverage.from-decile" "$WORK"/.coverage.desk* )
( cd "$DECILE" && uv run python -m coverage json --data-file="$WORK/.coverage" \
    -o "$DECILE/coverage.json" )

echo "==> the gate, over both"
( cd "$DECILE" && uv run python -m tools.coverage_gate --report coverage.json )
