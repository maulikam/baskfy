#!/usr/bin/env bash
# Tree 3 / G9 — a units bug (rupees vs crore) must not be able to pass silently.
#
# Market cap is stored in Rs crore. A factor-of-1e7 error would still sort correctly and would
# still bucket into deciles, so ordering proves nothing; only an absolute anchor does. These
# bands are deliberately wide — they exist to catch a wrong *unit*, not to pin a price.
set -uo pipefail
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
# Default to the date the product actually SERVES, not the newest bars on disk. The API
# resolves every as-of through `latest_published_date`, which is the newest pipeline_run
# carrying a data_version — 2026-08-18 here, three sessions behind max(ohlcv_daily.date).
# Filling only the newest bar date would leave every rendered surface on an em dash.
DATE="${1:-$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")}"
[ -z "$DATE" ] && DATE=$("${PSQL[@]}" "select max(date) from ohlcv_daily")

# symbol  min_cr  max_cr   — India's largest caps are Rs 3-25 lakh crore.
BANDS="RELIANCE 800000 3000000
TCS 400000 1800000
HDFCBANK 500000 2500000
INFY 200000 1200000"

fail=0; checked=0
while read -r symbol lo hi; do
  [ -z "$symbol" ] && continue
  value=$("${PSQL[@]}" "
    select f.marketcap_cr from fundamental_daily f
    join instrument i on i.id = f.instrument_id
    where i.symbol = '$symbol' and f.date = '$DATE'")
  if [ -z "$value" ]; then
    echo "  $symbol: no row for $DATE — not yet filled"
    continue
  fi
  checked=$((checked + 1))
  if [ "$value" -ge "$lo" ] && [ "$value" -le "$hi" ]; then
    echo "  $symbol: ${value} cr  in [$lo, $hi]  OK"
  else
    echo "  $symbol: ${value} cr  OUTSIDE [$lo, $hi]  <-- unit or parse bug"
    fail=1
  fi
done <<< "$BANDS"

# A second, independent check: marketcap_cr must equal shares * close_raw / 1e7 for every row,
# which catches a mis-join that the four anchors above would sail past.
drift=$("${PSQL[@]}" "
  select count(*) from fundamental_daily f
  join ohlcv_daily o on o.instrument_id = f.instrument_id and o.date = f.date
  where f.date = '$DATE'
    and f.shares_outstanding is not null
    and f.marketcap_cr is not null
    and abs(f.marketcap_cr - round(f.shares_outstanding * o.close_raw / 10000000)) > 1")
echo "  rows where marketcap_cr != shares * close_raw / 1e7: $drift"
[ "$drift" -ne 0 ] && fail=1

[ "$checked" -eq 0 ] && { echo "SANITY INCONCLUSIVE — no anchor symbols filled yet"; exit 1; }
[ "$fail" -eq 0 ] && echo "SANITY OK ($checked anchors, $drift arithmetic drifts)" || echo "SANITY FAILED"
exit "$fail"
