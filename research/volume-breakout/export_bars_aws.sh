#!/usr/bin/env bash
# Export the volume-breakout backtest tables from the AWS Phase-A box's Postgres
# (i-086986250704e4392, /opt/baskfy compose stack) via SSM, park them in S3 under
# backtests/volume-breakout/<stamp>/, and sync them down beside this script into ./aws/.
# Read-only against the database: every statement is COPY (SELECT ...) TO STDOUT.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}" AWS_REGION="${AWS_REGION:-ap-south-1}"
export BASKFY_INSTANCE_ID="${BASKFY_INSTANCE_ID:-i-086986250704e4392}"
BUCKET="${BASKFY_ARCHIVE_BUCKET:-baskfy-archive}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; KEY="backtests/volume-breakout/$STAMP"
aws sts get-caller-identity --query Account --output text >/dev/null || { echo "no AWS session — aws sso login --profile $AWS_PROFILE"; exit 1; }

C='cd /opt/baskfy && docker compose --env-file .env.staging.compose -f compose.prod.yml exec -T postgres psql -U baskfy -d baskfy -v ON_ERROR_STOP=1 -q'
copy() { echo "$C -c \"COPY ($2) TO STDOUT WITH (FORMAT csv, HEADER)\" | gzip -6 > /tmp/vb/$1.csv.gz; echo \"$1: \$(gzip -dc /tmp/vb/$1.csv.gz | wc -l) lines\""; }

echo "── preflight on the box"
bash "$ROOT/tools/deploy/box.sh" \
  "$C -tAc \"select count(*), min(date), max(date), count(distinct instrument_id) from ohlcv_daily\"" \
  "$C -tAc \"select count(*) from instrument\"" \
  "df -h /tmp | tail -1"

echo "── export on the box → /tmp/vb → s3://$BUCKET/$KEY/"
BOX_TIMEOUT_SECONDS=2400 bash "$ROOT/tools/deploy/box.sh" \
  "set -e; rm -rf /tmp/vb; mkdir -p /tmp/vb" \
  "$(copy instrument "SELECT id, symbol, name, isin, instrument_type, series, listed_on, delisted_on, is_active FROM instrument")" \
  "$(copy index_def "SELECT id, slug, name, is_universe FROM index_def")" \
  "$(copy index_member_daily "SELECT index_id, date, instrument_id FROM index_member_daily")" \
  "$(copy symbol_alias "SELECT instrument_id, old_symbol, changed_on FROM symbol_alias")" \
  "$(copy corporate_action "SELECT instrument_id, action_type, ex_date, ratio_from, ratio_to, amount FROM corporate_action")" \
  "$(copy trading_day "SELECT date, is_trading_day FROM trading_day WHERE exchange_id = 1")" \
  "$(copy market_health_daily "SELECT * FROM market_health_daily")" \
  "$(copy ohlcv_daily "SELECT instrument_id, date, open, high, low, close, volume, close_raw, volume_raw, adj_factor, upper_circuit, lower_circuit FROM ohlcv_daily ORDER BY instrument_id, date")" \
  "ls -la /tmp/vb; aws s3 cp --region $AWS_REGION --only-show-errors --recursive /tmp/vb s3://$BUCKET/$KEY/ && echo uploaded && rm -rf /tmp/vb"

echo "── sync down → $HERE/aws/"
mkdir -p "$HERE/aws"
aws s3 sync --only-show-errors "s3://$BUCKET/$KEY/" "$HERE/aws/"
ls -la "$HERE/aws/"
echo "done: s3://$BUCKET/$KEY/"
