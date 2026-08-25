#!/usr/bin/env bash
# Tree 3 — run the fundamentals fill to completion, restarting it when it stalls.
#
# Why a supervisor: the NSE fetch hangs roughly every ~600 requests. The socket stays
# ESTABLISHED and idle, and httpx's `read` timeout is per socket read rather than per request, so
# a server that trickles or goes silent mid-response is not timed out — the retry budget is never
# even reached. Diagnosed to that point, not past it; the provider-side fix is an open item.
#
# What makes restarting *correct* rather than a hack is the fill's own design: `--resume` skips
# symbols already stored, the raw-file archive never refetches a key it holds, and every batch of
# 25 is committed. So a restart costs the current batch and nothing else.
#
#   bash tools/tree3/drive-fill.sh 2026-08-18 [max_rounds] [seconds_per_round]
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
DATE="${1:?usage: drive-fill.sh YYYY-MM-DD [rounds] [seconds]}"
ROUNDS="${2:-12}"
PER_ROUND="${3:-900}"
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)

scope=$("${PSQL[@]}" "select count(*) from ohlcv_daily where date = '$DATE'")
echo "DRIVE date=$DATE scope=$scope rounds<=$ROUNDS budget=${PER_ROUND}s each"

for round in $(seq 1 "$ROUNDS"); do
  have=$("${PSQL[@]}" "select count(*) from fundamental_daily where date = '$DATE'")
  if [ "$have" -ge "$scope" ]; then
    echo "  round $round: $have/$scope — complete"
    break
  fi
  echo "  round $round: $have/$scope stored, running up to ${PER_ROUND}s"
  ( cd decile-blueprint && timeout "$PER_ROUND" \
      uv run python -m baskfy_worker.fundamentals_cli fill --date "$DATE" --resume \
      >> "/tmp/tree3-fill-$DATE.log" 2>&1 )
  code=$?
  after=$("${PSQL[@]}" "select count(*) from fundamental_daily where date = '$DATE'")
  echo "    exit=$code  stored $have -> $after"
  # A round that timed out AND stored nothing means restarting is not helping; stop rather
  # than spin. Anything else — progress, or a clean exit — continues.
  if [ "$after" -le "$have" ] && [ "$code" -ne 0 ]; then
    echo "    no progress this round; stopping so this does not spin"
    break
  fi
done

final=$("${PSQL[@]}" "select count(*) from fundamental_daily where date = '$DATE'")
echo "DRIVE COMPLETE date=$DATE stored=$final scope=$scope"
[ "$final" -ge "$scope" ] && echo "DRIVE OK" || echo "DRIVE PARTIAL ($((scope - final)) short)"
