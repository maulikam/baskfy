#!/usr/bin/env bash
# Tree 3 / G10 — compute_factors must actually pick the fundamentals up.
#
# `fundamental_daily` being full proves the fetch worked. It does not prove the product changed:
# every screener query, every column and every bucket reads `factor_daily`, and the join between
# them is keyed on (instrument_id, date) exactly. This checks the join, not the fetch.
set -uo pipefail
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
# Default to the date the product actually SERVES, not the newest bars on disk. The API
# resolves every as-of through `latest_published_date`, which is the newest pipeline_run
# carrying a data_version — 2026-08-18 here, three sessions behind max(ohlcv_daily.date).
# Filling only the newest bar date would leave every rendered surface on an em dash.
DATE="${1:-$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")}"
[ -z "$DATE" ] && DATE=$("${PSQL[@]}" "select max(date) from ohlcv_daily")

read -r rows non_null pe <<<"$("${PSQL[@]}" "
  select count(*), count(marketcap_cr), count(pe)
  from factor_daily where date = '$DATE'" | tr '|' ' ')"

echo "DATE $DATE"
echo "  factor_daily rows=$rows marketcap_cr=$non_null pe=$pe"

if [ "$rows" -eq 0 ]; then
  echo "JOIN NOT RUN — factor_daily has no rows for $DATE."
  echo "  run: cd decile-blueprint && uv run python -m baskfy_worker.factors_cli recompute --date $DATE"
  exit 1
fi

# The join must not have invented or dropped anything: every non-null cap in factor_daily has to
# match the fundamental_daily row it came from, exactly.
mismatch=$("${PSQL[@]}" "
  select count(*) from factor_daily f
  join fundamental_daily d
    on d.instrument_id = f.instrument_id and d.date = f.date
  where f.date = '$DATE'
    and f.marketcap_cr is distinct from d.marketcap_cr")
echo "  rows where factor_daily.marketcap_cr != fundamental_daily.marketcap_cr: $mismatch"

if [ "$non_null" -gt 0 ] && [ "$mismatch" -eq 0 ]; then
  echo "JOIN OK non_null=$non_null of $rows (pe=$pe)"
  exit 0
fi
echo "JOIN FAILED non_null=$non_null mismatch=$mismatch"
exit 1
