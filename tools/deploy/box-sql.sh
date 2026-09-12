#!/usr/bin/env bash
# Run one SQL statement against the box's Postgres and print the bare result.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*) from ohlcv_daily"
#
# WHY THIS EXISTS. A gate CHECK is handed to `sh -c` as a single line. Reaching the box's database
# from one means nesting three quoted layers — `box.sh "... docker compose exec -T postgres psql
# -c \"select ...\""` — and the innermost quotes collapse, so the command produces NO OUTPUT and
# the gate reads as a failure of the thing it was testing rather than of its own quoting.
# `gates/backfill-compression.md` G7 sat unmet that way. The SQL arrives here as one argv element,
# so nothing has to survive a second round of word splitting.
#
# Read-only by discipline, not by enforcement: this is the production database. Pass SELECTs.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: box-sql.sh <sql>" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
SQL="$1"
DB="${BASKFY_BOX_DB:-baskfy}"

# The SQL goes over as base64 so no quote in it has to survive SSM, the shell on the box, and
# `docker compose exec` in turn. It is decoded on the box and fed to psql on stdin.
B64="$(printf '%s' "$SQL" | base64 | tr -d '\n')"
bash "$ROOT/tools/deploy/box.sh" \
  "cd /opt/baskfy && echo $B64 | base64 -d | docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T postgres psql -U baskfy -d $DB -t -A -f -" \
  2>/dev/null | sed '/^$/d' | tail -5
