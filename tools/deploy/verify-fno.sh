#!/usr/bin/env bash
# FO12 — is the F&O book deployed dark and able to run its paper periods? (docs/fno/06 FO12:
# "Deploy … with every money flag still false"; the options pack's verify-options.sh, OP15.3).
# Two modes:
#
#   LOCAL=1 bash tools/deploy/verify-fno.sh      # the dev stack: this checkout + local Postgres
#     the compose pins (every FO money flag false, OPTIONS_ENABLED/INTRADAY pinned false, the
#     fno-monitor service), no FNO auto-execute name anywhere, the desk and the worker reading
#     every FO switch false with an empty environment, and the local schema at 0052 or later.
#
#   AWS_PROFILE=baskfy-poc bash tools/deploy/verify-fno.sh   # the box
#     over HTTPS: /api/v1/fno/overnight answers 401 without a token, the desk's /fno answers 401
#     (basic auth), never 5xx; over SSM: desk and fno-monitor are Up and agree, every FO money flag
#     false in both, OPTIONS_ENABLED and INTRADAY_ENABLED false, no FNO…AUTO variable, Beat carries
#     the FO entries, alembic at head, the latest fo_ingest_day, fo_sleeve_config seeded (F1
#     ₹25 lakh, F2 ₹0; DECISIONS-FO M.2).
#
# ASSERTED vs REPORTED. The money flags are the line between paper and money: asserted false in
# every mode (docs/fno/02 Track B; flipping one is 02 §3, Maulik's hand). The two operational flags
# — BASKFY_FNO_SCAN_ENABLED (worker: the nightly ingest and scans) and BASKFY_FNO_MONITOR_ENABLED
# (desk: the monitor; worker: the alerts and the weekly) — are operating choices: reported, never
# asserted, so a verify does not fail because the operator started the paper period.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/tools/deploy"
FAILS=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '  --   %s\n' "$*"; }
MONEY_FLAGS="BASKFY_FNO_CARRY_ENABLED BASKFY_FNO_F1_EXECUTION_ENABLED BASKFY_FNO_F2_EXECUTION_ENABLED BASKFY_FNO_F3_EXECUTION_ENABLED BASKFY_FNO_F3_AUTO_EXIT"
OPS_FLAGS="BASKFY_FNO_SCAN_ENABLED BASKFY_FNO_MONITOR_ENABLED"
COMPOSE="$ROOT/decile-blueprint/infra/docker/compose.prod.yml"
SCHEMA_AT_LEAST="0052"

# ------------------------------------------------------------------------------ both modes: repo
echo "── the repository"
for f in $MONEY_FLAGS BASKFY_FNO_MONITOR_ENABLED; do
  grep -q "      $f: \"\${$f:-false}\"" "$COMPOSE" && ok "compose pins $f default false" \
    || bad "compose does not pin $f to a false default"
done
grep -q '      OPTIONS_ENABLED: "false"' "$COMPOSE" && ok 'compose pins OPTIONS_ENABLED "false"' \
  || bad "compose does not pin OPTIONS_ENABLED false"
grep -q '      INTRADAY_ENABLED: "false"' "$COMPOSE" && ok 'compose pins INTRADAY_ENABLED "false"' \
  || bad "compose does not pin INTRADAY_ENABLED false"
# M.5 (Maulik, 28 Sep 2026): BASKFY_FNO_F3_AUTO_EXIT is the one admitted spelling — an exit only.
if grep -rEh 'FNO[A-Z0-9_]*AUTO' "$COMPOSE" "$ROOT/kite-momentum-rebalancer/app" \
     "$ROOT/kite-momentum-rebalancer/scripts" "$ROOT/decile-blueprint/services" \
     "$ROOT/decile-blueprint/packages" --include='*.py' --include='*.yml' --exclude-dir=tests \
     2>/dev/null | sed 's/BASKFY_FNO_F3_AUTO_EXIT//g' | grep -Eq 'FNO[A-Z0-9_]*AUTO'; then
  bad "an FNO…AUTO name other than BASKFY_FNO_F3_AUTO_EXIT appears in compose or code (02 Track B)"
else
  ok "no FNO…AUTO name but BASKFY_FNO_F3_AUTO_EXIT (M.5, an exit only) in compose, the desk, the services or the packages"
fi
grep -q "  fno-monitor:" "$COMPOSE" && grep -q "command: \[fno-monitor-loop\]" "$COMPOSE" \
  && ok "compose runs fno-monitor (fno-monitor-loop)" || bad "compose has no fno-monitor service"

if [ "${LOCAL:-0}" = "1" ]; then
  echo "── the dev stack"
  DESK_FLAGS="$(cd "$ROOT/kite-momentum-rebalancer" && env -i PATH="$PATH" HOME="$HOME" \
    .venv/bin/python -c 'from app import config as C
print(C.DRY_RUN, C.OPTIONS_ENABLED, C.INTRADAY_ENABLED, C.FNO_CARRY_ENABLED,
      C.FNO_F1_EXECUTION_ENABLED, C.FNO_F2_EXECUTION_ENABLED, C.FNO_F3_EXECUTION_ENABLED,
      C.FNO_F3_AUTO_EXIT, C.FNO_MONITOR_ENABLED)' 2>&1 | tail -1)"
  [ "$DESK_FLAGS" = "True False False False False False False False False" ] \
    && ok "desk defaults: DRY_RUN true, every FO switch false" \
    || bad "desk defaults are not safe: $DESK_FLAGS"
  GATES="$(cd "$ROOT/kite-momentum-rebalancer" && env -i PATH="$PATH" HOME="$HOME" \
    .venv/bin/python -c 'from baskfy_core.fno.config import FoSleeve
from app.fno_gates import fno_gates
print(" ".join(f"{s.value}={fno_gates(s).mode.value}" for s in FoSleeve))' 2>&1 | tail -1)"
  case "$GATES" in
    *LIVE*|"") bad "fno_gates with an empty environment: ${GATES:-unreadable}" ;;
    *) ok "fno_gates with an empty environment: $GATES" ;;
  esac
  WORKER_FLAGS="$(cd "$ROOT/decile-blueprint" && env -i PATH="$PATH" HOME="$HOME" \
    uv run --quiet python -c 'from baskfy_worker.settings import WorkerSettings
w = WorkerSettings(_env_file=None); print(w.fno_scan_enabled, w.fno_monitor_enabled)' 2>&1 | tail -1)"
  [ "$WORKER_FLAGS" = "False False" ] \
    && ok "worker defaults: FNO scan and monitor switches false" \
    || bad "worker defaults are not safe: $WORKER_FLAGS"
  PG="${BASKFY_LOCAL_PG:-baskfy-postgres}"; DB="${BASKFY_LOCAL_DB:-baskfy_test}"
  sql() { docker exec "$PG" psql -U baskfy -d "$DB" -tAc "$1" 2>/dev/null | tr -d ' '; }
  HEAD="$(sql 'select version_num from alembic_version')"
  case "$HEAD" in
    "") bad "cannot read alembic_version from $PG/$DB" ;;
    *) [[ ! "$HEAD" < "$SCHEMA_AT_LEAST" ]] && ok "local schema at $HEAD (the fo_ tables present)" \
         || bad "local schema $HEAD is older than the FO migration $SCHEMA_AT_LEAST" ;;
  esac
  note "latest fo_ingest_day: $(sql "select trade_date||':'||status from fo_ingest_day order by trade_date desc limit 1" | head -1 | grep . || echo 'none (a dev database has no F&O nights)')"
  note "fo_sleeve_config: $(sql "select string_agg(distinct sleeve||'='||capital_inr::bigint, ' ') from fo_sleeve_config" | grep . || echo 'not seeded locally')"
  echo
  [ "$FAILS" -eq 0 ] && echo "FNO OK (local) — every FO money flag false and pinned, no auto-execute name, schema at $HEAD" \
    || { echo "FNO FAIL (local) — $FAILS check(s) failed"; exit 1; }
  exit 0
fi

# ------------------------------------------------------------------------------ the box
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"
D="${BASKFY_DESK_URL:-https://desk.${B#https://}}"
crl()  { curl -s --max-time 40 "$@"; }
code() { crl -o /dev/null -w '%{http_code}' "$@"; }
want() { local label="$1" exp="$2"; shift 2; local got; got="$(code "$@")"
  [[ "$got" =~ ^($exp)$ ]] && ok "$label → $got" || bad "$label → $got (wanted $exp)"; }

echo "── over HTTPS: $B and $D"
want "GET /api/v1/fno/overnight without a token" 401 "$B/api/v1/fno/overnight"
want "GET desk /fno (basic auth)"                  401 "$D/fno"

echo "── over SSM"
C='cd /opt/baskfy && docker compose --env-file .env.staging.compose -f compose.prod.yml'
PSQL="$C exec -T postgres psql -U baskfy -d baskfy -tAc"
OUT="$(bash "$HERE/box.sh" \
  "echo '## ps'; $C ps --format '{{.Service}} {{.Status}}'" \
  "echo '## beat'; $C exec -T beat python -c 'from baskfy_worker.celery_app import app; print(chr(10).join(sorted(app.conf.beat_schedule)))'" \
  "echo '## desk-env'; $C exec -T desk env | grep -E '^(DRY_RUN|OPTIONS_ENABLED|INTRADAY_ENABLED|BASKFY_FNO_[A-Z0-9_]+)=' | sort" \
  "echo '## monitor-env'; $C exec -T fno-monitor env | grep -E '^(DRY_RUN|OPTIONS_ENABLED|INTRADAY_ENABLED|BASKFY_FNO_[A-Z0-9_]+)=' | sort" \
  "echo '## worker-env'; $C exec -T worker env | grep -E '^BASKFY_FNO_[A-Z0-9_]+=' | sort" \
  "echo '## alembic'; $PSQL 'select version_num from alembic_version'" \
  "echo '## ingest'; $PSQL \"select trade_date||':'||status from fo_ingest_day order by trade_date desc limit 1\"" \
  "echo '## sleeves'; $PSQL \"select distinct sleeve||'='||capital_inr::bigint from fo_sleeve_config order by 1\"" \
  "echo '## monitor-log'; $C logs --no-log-prefix --tail 5 fno-monitor" \
  2>&1)" || bad "box.sh failed:
$OUT"
section() { sed -n "/^## $1\$/,/^## /p" <<<"$OUT" | grep -v '^## '; }
for svc in desk fno-monitor worker beat; do
  section ps | grep -qE "^${svc} Up" && ok "$svc Up" || bad "$svc is not Up"
done
for entry in fno-bhavcopy fno-spread-sample fno-retest fno-weekly fno-alerts; do
  section beat | grep -qx "$entry" && ok "beat schedules $entry" || bad "beat schedule lacks $entry"
done
for svc in desk monitor; do
  ENV="$(section "$svc-env")"
  for f in OPTIONS_ENABLED INTRADAY_ENABLED $MONEY_FLAGS; do
    grep -qx "$f=false" <<<"$ENV" && ok "$svc: $f=false" || bad "$svc: $f is not false"
  done
  sed 's/BASKFY_FNO_F3_AUTO_EXIT=false//' <<<"$ENV" | grep -qE 'FNO[A-Z0-9_]*AUTO' \
    && bad "$svc: an FNO…AUTO variable other than BASKFY_FNO_F3_AUTO_EXIT=false is set" \
    || ok "$svc: no FNO…AUTO variable but BASKFY_FNO_F3_AUTO_EXIT=false (M.5)"
  note "$svc: $(grep -m1 '^DRY_RUN=' <<<"$ENV" || echo 'DRY_RUN unset') (the FO sleeves stay PAPER while OPTIONS_ENABLED is false)"
  note "$svc: $(grep -m1 '^BASKFY_FNO_MONITOR_ENABLED=' <<<"$ENV" || echo 'BASKFY_FNO_MONITOR_ENABLED unset')"
done
[ "$(section desk-env)" = "$(section monitor-env)" ] && ok "desk and fno-monitor agree on every FO flag" \
  || bad "desk and fno-monitor DISAGREE on the FO flags"
WENV="$(section worker-env)"
grep -qE 'FNO[A-Z0-9_]*AUTO' <<<"$WENV" && bad "worker: an FNO…AUTO variable is set" || ok "worker: no FNO…AUTO variable"
for f in $OPS_FLAGS; do note "worker: $(grep -m1 "^$f=" <<<"$WENV" || echo "$f unset (false)")"; done
HEAD="$(section alembic | tr -d ' ')"
[[ -n "$HEAD" && ! "$HEAD" < "$SCHEMA_AT_LEAST" ]] && ok "alembic at $HEAD (the fo_ tables present)" \
  || bad "alembic ${HEAD:-unreadable} is older than the FO migration $SCHEMA_AT_LEAST"
note "latest fo_ingest_day: $(section ingest | tr -d ' ' | grep . || echo 'none yet (BASKFY_FNO_SCAN_ENABLED off, or no night run)')"
SLEEVES="$(section sleeves | tr -d ' ')"
grep -qx 'F1=2500000' <<<"$SLEEVES" && ok "fo_sleeve_config: F1 ₹25,00,000" \
  || bad "fo_sleeve_config: F1 is not ₹25 lakh (got: $(tr '\n' ' ' <<<"$SLEEVES"))"
grep -qx 'F2=0' <<<"$SLEEVES" && ok "fo_sleeve_config: F2 ₹0" \
  || bad "fo_sleeve_config: F2 is not ₹0 (got: $(tr '\n' ' ' <<<"$SLEEVES"))"
note "fno-monitor: $(section monitor-log | tail -1)"

echo
[ "$FAILS" -eq 0 ] && echo "FNO OK — $B, $D: every FO money flag false in desk and fno-monitor, no auto-execute, monitor Up, alembic $HEAD" \
  || { echo "FNO FAIL — $FAILS check(s) failed"; exit 1; }
