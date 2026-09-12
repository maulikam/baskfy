#!/usr/bin/env bash
# Read the box's bars that the research panel does not have, so leaf 6's measurement can run.
#
#     AWS_PROFILE=baskfy-poc bash tools/twt/twt-chartink-pull.sh 2026-09-10 2026-09-11
#
# READ-ONLY. Two SELECTs and nothing else — no INSERT, no migration, no deploy, no container
# restart (PLAN-SCAN-SYNC hard rules 1 and 4). The only thing it writes on the box is a file under
# /tmp, which it removes before it exits.
#
# WHY IT PAGES. `box.sh` goes over SSM `send-command`, whose StandardOutputContent is capped at
# 24,000 characters. 8,736 bars do not fit in one response, so the result is gzipped and base64'd
# on the box and `cut` into 20,000-character pages. The row count is asserted at the end: a page
# lost in transit shows up as a short file rather than as a quietly smaller universe.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
OUT="$ROOT/decile-blueprint/data/outputs/twt"
mkdir -p "$OUT"
FIRST="${1:-2026-09-10}"; LAST="${2:-2026-09-11}"
DB="${BASKFY_BOX_DB:-baskfy}"
PSQL="cd /opt/baskfy && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T postgres psql -U baskfy -d $DB -t -A -F'|'"

fetch() { # <sql> <destination>
  local remote="/tmp/twt_pull_$$.b64"
  local size pages i start end
  size="$(bash "$ROOT/tools/deploy/box.sh" \
    "$PSQL -c \"$1\" | gzip -9 | base64 -w0 > $remote; wc -c < $remote" 2>/dev/null | tr -d ' \n')"
  echo "  $2: $size base64 characters on the box"
  pages=$(( (size + 19999) / 20000 ))
  : > "$2.b64"
  for (( i=0; i<pages; i++ )); do
    start=$(( i * 20000 + 1 )); end=$(( (i + 1) * 20000 ))
    bash "$ROOT/tools/deploy/box.sh" "cut -c${start}-${end} $remote" 2>/dev/null \
      | tr -d '\n ' >> "$2.b64"
  done
  bash "$ROOT/tools/deploy/box.sh" "rm -f $remote" >/dev/null 2>&1
  [ "$(wc -c < "$2.b64" | tr -d ' ')" = "$size" ] || { echo "short read for $2" >&2; exit 1; }
  base64 -d < "$2.b64" | gunzip > "$2"
  rm -f "$2.b64"
  echo "  $2: $(wc -l < "$2" | tr -d ' ') bars"
}

BARS="select i.symbol, o.date, o.low, o.close, o.close_raw, o.volume from ohlcv_daily o join instrument i on i.id=o.instrument_id"

echo "topping the panel up with $FIRST .. $LAST"
fetch "$BARS where o.date between '$FIRST' and '$LAST' order by 1,2" "$OUT/box_topup.psv"

# The repair set: every name Chartink lists on the last session, so a name the 1.7 GB panel is
# thinner about than the plant is not scored as a miss the scan is responsible for.
NAMES="$(python3 - "$ROOT" "$LAST" <<'PY'
import csv, io, sys
root, last = sys.argv[1], sys.argv[2]
day = "-".join(reversed(last.split("-")))
rows = csv.reader(io.open(f"{root}/research/tight-close/chartink_backtest.csv", encoding="utf-8-sig"))
print(",".join(sorted({f"'{r[1]}'" for r in rows if r and r[0] == day})))
PY
)"
echo "repairing ${NAMES//,/ } from 2026-04-01"
fetch "$BARS where i.symbol in ($NAMES) and o.date >= '2026-04-01' order by 1,2" "$OUT/box_repair.psv"
echo "done. now: cd decile-blueprint && uv run python ../tools/twt/twt_chartink_gap.py measure --session $LAST"
