#!/usr/bin/env bash
# G5 — staging holds real market data and a published run.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
ID="${BASKFY_INSTANCE_ID:-i-086986250704e4392}"
q() { BASKFY_INSTANCE_ID="$ID" bash "$ROOT/tools/deploy/box.sh" \
  "cd /opt/baskfy && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T postgres psql -U baskfy -d baskfy -tAc \"$1\"" \
  2>/dev/null | tr -d ' \r\n'; }

OHLCV="$(q 'select count(*) from ohlcv_daily')"
FACTOR="$(q 'select count(*) from factor_daily')"
INSTR="$(q 'select count(*) from instrument')"
PUBLISHED="$(q "select count(*) from pipeline_run where status='succeeded' and data_version is not null")"

# Thresholds, not exact counts: a later backfill should not fail this gate. The floors are an
# order of magnitude below what was loaded, so they catch "empty" and not "different".
[ "${OHLCV:-0}" -gt 1000000 ]  || fail "ohlcv_daily has ${OHLCV:-0} rows"
[ "${FACTOR:-0}" -gt 100000 ]  || fail "factor_daily has ${FACTOR:-0} rows"
[ "${INSTR:-0}" -gt 5000 ]     || fail "instrument has ${INSTR:-0} rows"
[ "${PUBLISHED:-0}" -ge 1 ]    || fail "no published pipeline_run — the API will answer 503 pipeline-degraded"

# And no credential ever travelled with it. The seed was market data only, by construction.
for t in refresh_token auth_token; do
  n="$(q "select count(*) from $t")"
  [ "${n:-0}" -lt 50 ] || fail "$t has ${n} rows — a full dump was restored, not the market subset"
done
echo "DATA OK — ohlcv ${OHLCV}, factor ${FACTOR}, instruments ${INSTR}, ${PUBLISHED} published run"
