#!/usr/bin/env bash
# Tree 3 / G11 — market-cap decile bucketing must no longer be degraded.
#
# The screener buckets with `percent_rank() over (order by marketcap_cr desc nulls last)`
# (packages/core/src/baskfy_core/screener.py:412). When every cap is NULL that ordering is one
# giant tie, so "D1 = the largest 10%" silently returns an arbitrary 10% — which is exactly the
# degradation NEEDS-MAULIK §15 describes. The test is therefore not "are there buckets" but
# "does D1 actually contain the biggest companies".
set -uo pipefail
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
# Default to the date the product actually SERVES, not the newest bars on disk. The API
# resolves every as-of through `latest_published_date`, which is the newest pipeline_run
# carrying a data_version — 2026-08-18 here, three sessions behind max(ohlcv_daily.date).
# Filling only the newest bar date would leave every rendered surface on an em dash.
DATE="${1:-$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")}"
[ -z "$DATE" ] && DATE=$("${PSQL[@]}" "select max(date) from ohlcv_daily")

echo "DATE $DATE"
echo "  bucket sizes and cap ranges (deciles of the ranking the screener uses):"
"${PSQL[@]}" "
  with ranked as (
    select marketcap_cr,
           ntile(10) over (order by marketcap_cr desc nulls last) as bucket
    from factor_daily where date = '$DATE'
  )
  select 'D' || bucket, count(*), count(marketcap_cr),
         coalesce(max(marketcap_cr)::text,'-'), coalesce(min(marketcap_cr)::text,'-')
  from ranked group by bucket order by bucket" \
| awk -F'|' '{printf "    %-4s n=%-6s with_cap=%-6s max=%-10s min=%s\n", $1,$2,$3,$4,$5}'

# The ordering property: no company outside D1 may be larger than the smallest company in D1.
read -r d1_min rest_max <<<"$("${PSQL[@]}" "
  with ranked as (
    select marketcap_cr,
           ntile(10) over (order by marketcap_cr desc nulls last) as bucket
    from factor_daily where date = '$DATE' and marketcap_cr is not null
  )
  select min(marketcap_cr) filter (where bucket = 1),
         max(marketcap_cr) filter (where bucket > 1)
  from ranked" | tr '|' ' ')"

distinct=$("${PSQL[@]}" "
  select count(distinct marketcap_cr) from factor_daily
  where date = '$DATE' and marketcap_cr is not null")

echo "  smallest cap in D1 = ${d1_min:-none};  largest cap outside D1 = ${rest_max:-none}"
echo "  distinct marketcap_cr values = $distinct"

if [ -z "$d1_min" ] || [ -z "$rest_max" ]; then
  echo "BUCKETS FAILED — no non-null caps to bucket on $DATE"
  exit 1
fi
if [ "$d1_min" -ge "$rest_max" ] && [ "$distinct" -gt 100 ]; then
  echo "BUCKETS OK distinct=$distinct d1_min=$d1_min >= rest_max=$rest_max"
  exit 0
fi
echo "BUCKETS FAILED — D1 is not the largest decile, or the ranking is nearly all ties"
exit 1
