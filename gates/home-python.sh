#!/usr/bin/env bash
# G18 for GATES.md (the home dashboard, tree 3).
#
# The Python half, in one command so the gate has one thing to read: the new suites pass, the
# siblings this tree touched still pass, and every file it owns is ruff / ruff-format / mypy
# clean. Prints PY_OK only when all three hold; otherwise prints which step failed.
set -uo pipefail

ROOT="/Users/maulikdave/Documents/projects/baskfy/decile-blueprint"
cd "$ROOT" || { echo "PY_FAILED cd"; exit 1; }

# A database of this gate's own, on the same server as `.env`'s.
#
# The db-backed fixtures here run `DROP SCHEMA public CASCADE` + `alembic upgrade head`, which is
# not safe to share: with other sessions running pytest against `baskfy_test` at the same time,
# two resets interleave and one loses with `duplicate key ... (typname)=(alembic_version)`. That
# is a shared-resource race, not a defect in anything under test, and it made the gate report red
# for a reason the gate is not about. Same server, same credentials, own database.
BASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')"
BASKFY_TEST_DATABASE_URL="${BASKFY_HOME_GATE_DATABASE_URL:-${BASE_URL%/*}/baskfy_home_test}"
export BASKFY_TEST_DATABASE_URL

# Same reasoning for Redis: `api_helpers.api_settings` defaults to `redis://localhost:6380/0`, and
# the suite's `clean_redis_namespaces` fixture deletes keys in it. Two sessions on database 0 flush
# each other's rate-limit and cache keys mid-test. Database 5 is this gate's.
export BASKFY_REDIS_URL="${BASKFY_HOME_GATE_REDIS_URL:-redis://localhost:6380/5}"

OWNED_SRC=(
  packages/core/src/baskfy_core/curated_trending.py
  services/api/src/baskfy_api/routers/curated_trending.py
  services/api/src/baskfy_api/app.py
)
OWNED_TESTS=(
  packages/core/tests/test_curated_trending.py
  services/api/tests/test_curated_trending_http.py
)

# The new suites, plus the siblings a new router and a regenerated client could have broken.
SUITES=(
  packages/core/tests/test_curated_trending.py
  packages/core/tests/test_no_escape_hatches.py
  services/api/tests/test_curated_trending_http.py
  services/api/tests/test_curated_engage.py
  services/api/tests/test_explore_http.py
  services/api/tests/test_explore_catalog.py
  services/api/tests/test_explore_no_orders.py
)

OUTPUT="$(uv run pytest "${SUITES[@]}" -p no:randomly 2>&1)"
STATUS=$?
COUNT="$(printf '%s\n' "$OUTPUT" | grep -oE '[0-9]+ passed' | tail -1)"
if [ "$STATUS" -ne 0 ] || [ -z "$COUNT" ]; then
  echo "PY_FAILED tests"
  printf '%s\n' "$OUTPUT" | tail -8
  exit 1
fi

uv run ruff check "${OWNED_SRC[@]}" "${OWNED_TESTS[@]}" >/dev/null 2>&1 || { echo "PY_FAILED ruff"; exit 1; }
uv run ruff format --check "${OWNED_SRC[@]}" "${OWNED_TESTS[@]}" >/dev/null 2>&1 || { echo "PY_FAILED format"; exit 1; }
uv run mypy packages/core/src services/api/src "${OWNED_TESTS[@]}" >/dev/null 2>&1 || { echo "PY_FAILED mypy"; exit 1; }

echo "suites: $COUNT"
echo "PY_OK"
