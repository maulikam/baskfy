#!/usr/bin/env bash
# Refresh data/ from the local Baskfy Postgres (container baskfy-local-postgres-1, db baskfy).
# Read-only: every statement is COPY (SELECT ...) TO STDOUT. Run on the Mac with the stack up:
#   bash machinelearning/python/research/portfolio-ml/export_bars_local.sh
# Then `python build_dataset.py && python run_research.py --rescore` from this folder.
set -euo pipefail
OUT="$(cd "$(dirname "$0")" && pwd)/data"
mkdir -p "$OUT"
PG="docker exec -i ${BASKFY_PG_CONTAINER:-baskfy-local-postgres-1} psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -q"
copy() { echo "exporting $1 ..."; $PG -c "COPY ($2) TO STDOUT WITH (FORMAT csv, HEADER)" | gzip -6 > "$OUT/$1.csv.gz"; echo "  $(gzip -dc "$OUT/$1.csv.gz" | wc -l | tr -d ' ') lines"; }

copy instrument          "SELECT id, symbol, name, isin, instrument_type, series, listed_on, delisted_on, is_active FROM instrument"
copy index_def           "SELECT id, slug, name, is_universe FROM index_def"
copy index_member_daily  "SELECT index_id, date, instrument_id FROM index_member_daily"
copy symbol_alias        "SELECT instrument_id, old_symbol, changed_on FROM symbol_alias"
copy corporate_action    "SELECT instrument_id, action_type, ex_date, ratio_from, ratio_to, amount FROM corporate_action"
copy market_health_daily "SELECT index_id, date, pct_above_200dma, pct_above_50dma, pct_within_10pct_ath, pct_ret_1y_positive, constituent_count, pct_above_20dma FROM market_health_daily"
copy ohlcv_daily         "SELECT instrument_id, date, open, high, low, close, volume, close_raw, volume_raw, adj_factor, upper_circuit, lower_circuit FROM ohlcv_daily ORDER BY instrument_id, date"
# benchmark closes: index_bar_daily (2011→, keyed by index_master.id, NOT index_def.id); mom50 has no bars, so snapshot level
$PG -c "COPY (SELECT m.kite_tradingsymbol AS index_name, b.date, b.open, b.high, b.low, b.close FROM index_bar_daily b JOIN index_master m ON m.id = b.index_id WHERE m.kite_tradingsymbol IN ('NIFTY 50','NIFTY MIDCAP 150','NIFTY SMLCAP 250') UNION ALL SELECT 'NIFTY 500 MOMENTUM 50', s.date, s.level, s.level, s.level, s.level FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id WHERE d.slug = 'nifty500-momentum-50' AND s.level IS NOT NULL ORDER BY 1,2) TO STDOUT WITH (FORMAT csv, HEADER)" > "$OUT/index_series.csv"
# PM1: present once 0002 has run and the backfill has written it
if $PG -At -c "SELECT to_regclass('public.fundamental_pit')" | grep -q fundamental_pit; then
  copy fundamental_pit "SELECT instrument_id, period_end, filed_on, period_type, filed_at, available_on, is_revision, revenue, other_income, finance_costs, depreciation, ebitda, pbt, pat, eps_diluted, shares_outstanding, total_debt, total_equity, cfo, capex, source, source_url FROM fundamental_pit"
fi
if $PG -At -c "SELECT to_regclass('public.shareholding_pit')" | grep -q shareholding_pit; then
  copy shareholding_pit "SELECT instrument_id, period_end, filed_on, filed_at, available_on, is_revision, promoter_pct, promoter_pledged_pct, fii_pct, dii_pct, mf_pct, public_pct, n_shareholders, source FROM shareholding_pit"
fi
copy fundamental_daily "SELECT instrument_id, date, marketcap_cr FROM fundamental_daily"
if $PG -At -c "SELECT to_regclass('public.instrument_sector')" | grep -q instrument_sector; then
  copy instrument_sector "SELECT instrument_id, macro, sector, industry, basic_industry, as_of FROM instrument_sector"
fi
if $PG -At -c "SELECT to_regclass('public.instrument_terminal')" | grep -q instrument_terminal; then
  copy instrument_terminal "SELECT instrument_id, status, reason, last_bar, source FROM instrument_terminal"
fi
rm -f "$OUT/ohlcv_daily.parquet" "$OUT/dataset.parquet" "$OUT/features.parquet"
echo "done: $(du -sh "$OUT" | cut -f1)"
