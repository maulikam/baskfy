#!/usr/bin/env bash
# Tree 3 / G8 — coverage as a fraction with its denominator, never as an implication.
set -uo pipefail
PSQL=(docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc)
# Default to the date the product actually SERVES, not the newest bars on disk. The API
# resolves every as-of through `latest_published_date`, which is the newest pipeline_run
# carrying a data_version — 2026-08-18 here, three sessions behind max(ohlcv_daily.date).
# Filling only the newest bar date would leave every rendered surface on an em dash.
DATE="${1:-$("${PSQL[@]}" "select max(trade_date) from pipeline_run where data_version is not null")}"
[ -z "$DATE" ] && DATE=$("${PSQL[@]}" "select max(date) from ohlcv_daily")

read -r scoped mcap pe shares <<<"$("${PSQL[@]}" "
  select
    count(*),
    count(f.marketcap_cr),
    count(f.pe),
    count(f.shares_outstanding)
  from ohlcv_daily o
  join instrument i on i.id = o.instrument_id
  left join fundamental_daily f
    on f.instrument_id = o.instrument_id and f.date = o.date
  where o.date = '$DATE'" | tr '|' ' ')"

pct() { [ "$2" -eq 0 ] && { echo "0.0"; return; }; awk -v a="$1" -v b="$2" 'BEGIN{printf "%.1f", 100*a/b}'; }

echo "DATE $DATE  (denominator = instruments with a bar that day)"
echo "COVERAGE marketcap_cr=$mcap/$scoped ($(pct "$mcap" "$scoped")%)"
echo "COVERAGE pe=$pe/$scoped ($(pct "$pe" "$scoped")%)"
echo "COVERAGE shares_outstanding=$shares/$scoped ($(pct "$shares" "$scoped")%)"
echo
echo "  not covered (no fundamental_daily row at all):"
"${PSQL[@]}" "
  select count(*) from ohlcv_daily o
  left join fundamental_daily f on f.instrument_id = o.instrument_id and f.date = o.date
  where o.date = '$DATE' and f.instrument_id is null" | sed 's/^/    missing_rows=/'
echo "  rows present but P/E null (names without earnings — an em dash is correct):"
"${PSQL[@]}" "
  select count(*) from fundamental_daily
  where date = '$DATE' and pe is null" | sed 's/^/    pe_null=/'
