#!/usr/bin/env bash
# PM0.7: export the dead from the throwaway database (never the running stack) and merge with data/.
#   bash export_dead_local.sh <output_dir>     (BASKFY_PG_CONTAINER, default pm07-pg)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; OUT="$1"; DEAD="$OUT/_dead"; mkdir -p "$DEAD"
PG="docker exec -i ${BASKFY_PG_CONTAINER:-pm07-pg} psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -q"
copy() { $PG -c "COPY ($2) TO STDOUT WITH (FORMAT csv, HEADER)" | gzip -6 > "$DEAD/$1.csv.gz"; echo "$1: $(gzip -dc "$DEAD/$1.csv.gz" | wc -l | tr -d ' ') lines"; }
copy instrument "SELECT id, symbol, name, isin, instrument_type, series, listed_on, delisted_on, is_active FROM instrument"
copy instrument_terminal "SELECT instrument_id, status, reason, last_bar, source FROM instrument_terminal"
copy dead_bars "SELECT o.instrument_id, o.date, o.open, o.high, o.low, o.close, o.volume, o.close_raw, o.volume_raw, o.adj_factor FROM ohlcv_daily o JOIN instrument_terminal t USING (instrument_id) WHERE t.source <> 'BARS_STOPPED' ORDER BY 1, 2"
cd "$HERE" && .venv/bin/python merge_dead_export.py "$DEAD" "$OUT"
