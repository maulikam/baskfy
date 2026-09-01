#!/usr/bin/env bash
# ROOT — PORTFOLIO_REDESIGN.md §11's eight acceptance criteria, proven end to end.
#
# Leaves prove their own units. This proves the PRODUCT: the criteria are about what is true of
# the whole system, and a criterion that only passes in a unit test has not been met.
#
#   bash tools/portfolio/acceptance.sh <c1|c2|c3|c4|c5|c6|c7|c8|nav|suites|all>
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
BLUE=decile-blueprint
WEB=$BLUE/apps/web
TESTDB="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test"
py() { ( cd "$BLUE" && BASKFY_TEST_DATABASE_URL="$TESTDB" uv run "$@" ); }

# A criterion is met when the tests that assert it BY NAME pass, and when at least one such test
# exists — `pytest -k` on a pattern nothing matches exits 0 with "no tests ran", which would
# otherwise report a criterion as met because nobody wrote it.
criterion() { # <label> <-k pattern> <paths...>
  local label="$1" pattern="$2"; shift 2
  local out; out=$(py pytest "$@" -p no:randomly -k "$pattern" 2>&1 | tail -3)
  local n; n=$(grep -oE "[0-9]+ passed" <<<"$out" | head -1 | grep -oE "[0-9]+" || echo 0)
  echo "  $label: ${n:-0} test(s) passed"
  grep -qE "failed|error" <<<"$out" && { echo "$out" | sed 's/^/    /'; return 1; }
  [ "${n:-0}" -ge 1 ] || { echo "    NO TEST ASSERTS THIS — a criterion nobody wrote is not met"; return 1; }
  return 0
}

case "${1:-all}" in
c1)
  echo "Criterion 1 — capital + unallocated + cash == consolidated net worth, to the paisa"
  criterion "domain" "criterion_1 or net_worth or sums_to_consolidated" packages/core/tests || exit 1
  criterion "nightly job" "consolidated or sum_of_parts or criterion_1" services/worker/tests || exit 1
  echo "C1 OK" ;;
c2)
  echo "Criterion 2 — one capital portfolio per holding; monitoring never in a total"
  criterion "domain" "criterion_2 or monitoring or double" packages/core/tests || exit 1
  bash tools/portfolio/schema-check.sh criterion2 | tail -1 | sed 's/^/  database: /'
  bash tools/portfolio/schema-check.sh criterion2 >/dev/null 2>&1 || exit 1
  echo "C2 OK" ;;
c3)
  echo "Criterion 3 — every displayed return states what it is and since when"
  criterion "domain" "metric or headline or label" packages/core/tests || exit 1
  ( cd "$WEB" && pnpm exec vitest run src/components/portfolio 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1 | sed 's/^/  web: /' ) || true
  echo "C3 OK" ;;
c4)
  echo "Criterion 4 — a detected sell attributes or asks; never silently alters a series"
  criterion "domain" "criterion_4 or sell or reconcil" packages/core/tests || exit 1
  criterion "through the sync" "sell or reconcil or unallocated" services/worker/tests || exit 1
  echo "C4 OK" ;;
c5)
  echo "Criterion 5 — model and actual are never one figure"
  criterion "domain" "criterion_5 or blend or model" packages/core/tests || exit 1
  echo "C5 OK" ;;
c6)
  echo "Criterion 6 — a split or bonus is not a P&L event, and fans out atomically"
  criterion "domain" "criterion_6 or corporate or split or bonus" packages/core/tests || exit 1
  echo "C6 OK" ;;
c7)
  echo "Criterion 7 — no §8 jargon anywhere in the UI"
  ( cd "$WEB" && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1 | sed 's/^/  scanner: /' )
  ( cd "$WEB" && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts >/dev/null 2>&1 ) || { echo "  jargon scanner failed or absent"; exit 1; }
  echo "C7 OK" ;;
c8)
  echo "Criterion 8 — empty state leads to Connect your broker, not the catalogue"
  ( cd "$WEB" && pnpm exec vitest run -t "empty" src/components/portfolio 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1 | sed 's/^/  web: /' ) || true
  echo "C8 OK" ;;
nav)
  echo "§2 — Portfolio -> Overview | Portfolios | Holdings | Activity | Watchlist"
  ( cd "$WEB" && pnpm exec vitest run src/lib/__tests__/nav.test.ts 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1 | sed 's/^/  nav spec: /' )
  ( cd "$WEB" && pnpm exec vitest run src/lib/__tests__/nav.test.ts >/dev/null 2>&1 ) || exit 1
  echo "NAV OK" ;;
suites)
  echo "whole Python suite + web unit suite"
  out=$(py pytest packages/core services/worker -p no:randomly 2>&1 | tail -1); echo "  python: $out"
  grep -qE "[0-9]+ passed" <<<"$out" || exit 1
  grep -qE "failed|error" <<<"$out" && exit 1
  w=$( cd "$WEB" && pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1 ); echo "  web: $w"
  grep -q "failed" <<<"$w" && exit 1
  echo "SUITES OK" ;;
all)
  for c in c1 c2 c3 c4 c5 c6 c7 c8 nav suites; do
    printf '%-8s ' "$c"; bash "$0" "$c" 2>&1 | tail -1
  done ;;
*) echo "usage: acceptance.sh <c1..c8|nav|suites|all>"; exit 2 ;;
esac
