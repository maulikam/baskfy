#!/usr/bin/env bash
# Export the tables the volume-breakout backtest needs from the local Baskfy Postgres
# (docker container baskfy-postgres, db baskfy) into gzipped CSVs beside this script.
# Read-only: every statement is a COPY (SELECT ...) TO STDOUT. Nothing is written to the DB.
set -euo pipefail
OUT="$(cd "$(dirname "$0")" && pwd)"
PSQL="docker exec -i baskfy-postgres psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -q -At"
copy() { local name="$1" sql="$2"; echo "exporting $name ..."; docker exec -i baskfy-postgres psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -q -c "COPY ($sql) TO STDOUT WITH (FORMAT csv, HEADER)" | gzip -6 > "$OUT/$name.csv.gz"; echo "  $(gzip -dc "$OUT/$name.csv.gz" | wc -l | tr -d ' ') lines -> $name.csv.gz"; }

copy instrument   "SELECT id, symbol, name, isin, instrument_type, series, listed_on, delisted_on, is_active FROM instrument"
copy index_def    "SELECT id, slug, name, is_universe FROM index_def"
copy index_member_daily "SELECT index_id, date, instrument_id FROM index_member_daily"
copy symbol_alias "SELECT instrument_id, old_symbol, changed_on FROM symbol_alias"
copy corporate_action "SELECT instrument_id, action_type, ex_date, ratio_from, ratio_to, amount FROM corporate_action"
copy trading_day  "SELECT date, is_trading_day FROM trading_day WHERE exchange_id = 1"
copy ohlcv_daily  "SELECT instrument_id, date, open, high, low, close, volume, close_raw, volume_raw, adj_factor, upper_circuit, lower_circuit FROM ohlcv_daily ORDER BY instrument_id, date"
echo "done: $(ls -la "$OUT"/*.csv.gz | awk '{s+=$5} END {print s/1048576 " MB total"}')"
