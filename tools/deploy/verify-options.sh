#!/usr/bin/env bash
# OP15 — is the options book deployed dark and able to run its paper periods? (docs/options/06 OP15:
# "verify-options.sh (flags, next expiry, token state)"). Two modes:
#
#   LOCAL=1 bash tools/deploy/verify-options.sh      # the dev stack: this checkout + local Postgres
#     the repo's defaults and compose pins (every money flag false, OPTIONS_ENABLED/INTRADAY pinned
#     false, no options auto-execute variable anywhere), the desk and worker reading their flags
#     false with an empty environment, the options schema at head on the local database, and the
#     next NIFTY expiry the local master knows (reported; a fresh dev database has none).
#
#   AWS_PROFILE=baskfy-poc bash tools/deploy/verify-options.sh   # the box
#     over HTTPS: /api/v1/options/today answers 401 without a token, the desk's /nifty-options
#     answers 401 (basic auth), never 5xx; over SSM: desk and options-monitor are Up and agree,
#     every options execution flag is false in both, OPTIONS_ENABLED and INTRADAY_ENABLED false, no
#     OPTIONS*AUTO variable in either, Beat carries the options entries, alembic at head, the next
#     NIFTY expiry from op_expiry, and the Kite token state from the desk's /status.
#
# ASSERTED vs REPORTED. The money flags are the line between paper and money: asserted false in
# every mode (02 Track B; flipping one is 02 §3, Maulik's hand). The three operational flags —
# monitor, collect, scan — are operating choices (PACK.11): reported, never asserted, so a verify
# does not fail because the operator turned the collector on.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/tools/deploy"
FAILS=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '  --   %s\n' "$*"; }
MONEY_FLAGS="BASKFY_OPTIONS_O1M_EXECUTION_ENABLED BASKFY_OPTIONS_O1W_EXECUTION_ENABLED BASKFY_OPTIONS_O2_EXECUTION_ENABLED BASKFY_OPTIONS_O3_EXECUTION_ENABLED"
OPS_FLAGS="BASKFY_OPTIONS_MONITOR_ENABLED BASKFY_OPTIONS_COLLECT_ENABLED BASKFY_OPTIONS_SCAN_ENABLED"
COMPOSE="$ROOT/decile-blueprint/infra/docker/compose.prod.yml"

# ------------------------------------------------------------------------------ both modes: repo
echo "── the repository"
for f in $MONEY_FLAGS BASKFY_OPTIONS_MONITOR_ENABLED; do
  grep -q "      $f: \"\${$f:-false}\"" "$COMPOSE" && ok "compose pins $f default false" \
    || bad "compose does not pin $f to a false default"
done
grep -q '      OPTIONS_ENABLED: "false"' "$COMPOSE" && ok 'compose pins OPTIONS_ENABLED "false"' \
  || bad "compose does not pin OPTIONS_ENABLED false"
grep -q '      INTRADAY_ENABLED: "false"' "$COMPOSE" && ok 'compose pins INTRADAY_ENABLED "false"' \
  || bad "compose does not pin INTRADAY_ENABLED false"
if grep -rEq 'OPTIONS[A-Z0-9_]*AUTO' "$COMPOSE" "$ROOT/kite-momentum-rebalancer/app" \
     "$ROOT/decile-blueprint/services" --include='*.py' --include='*.yml' 2>/dev/null; then
  bad "an OPTIONS…AUTO name appears in compose or code (02 Track B: there is none)"
else
  ok "no OPTIONS…AUTO name in compose, the desk or the services"
fi
grep -q "options-monitor:" "$COMPOSE" && ok "compose runs options-monitor" \
  || bad "compose has no options-monitor service"

if [ "${LOCAL:-0}" = "1" ]; then
  echo "── the dev stack"
  DESK_FLAGS="$(cd "$ROOT/kite-momentum-rebalancer" && env -i PATH="$PATH" HOME="$HOME" \
    .venv/bin/python -c 'from app import config as C
print(C.DRY_RUN, C.OPTIONS_ENABLED, C.INTRADAY_ENABLED, C.OPTIONS_O1M_EXECUTION_ENABLED,
      C.OPTIONS_O1W_EXECUTION_ENABLED, C.OPTIONS_O2_EXECUTION_ENABLED, C.OPTIONS_O3_EXECUTION_ENABLED)' 2>&1 | tail -1)"
  [ "$DESK_FLAGS" = "True False False False False False False" ] \
    && ok "desk defaults: DRY_RUN true, every options switch false" \
    || bad "desk defaults are not safe: $DESK_FLAGS"
  WORKER_FLAGS="$(cd "$ROOT/decile-blueprint" && env -i PATH="$PATH" HOME="$HOME" \
    uv run --quiet python -c 'from baskfy_worker.options import options_flags
f = options_flags(); print(f.dry_run, f.options_enabled, f.intraday_enabled, f.o1m_execution_enabled,
      f.o1w_execution_enabled, f.o2_execution_enabled, f.o3_execution_enabled)' 2>&1 | tail -1)"
  [ "$WORKER_FLAGS" = "True False False False False False False" ] \
    && ok "worker defaults: dry run, every options switch false" \
    || bad "worker defaults are not safe: $WORKER_FLAGS"
  # The dev stack's Postgres is the compose container (infra/docker/compose.yml); ask it directly,
  # so this needs no psql on the laptop. BASKFY_LOCAL_PG / BASKFY_LOCAL_DB override the names.
  PG="${BASKFY_LOCAL_PG:-baskfy-postgres}"; DB="${BASKFY_LOCAL_DB:-baskfy_test}"
  sql() { docker exec "$PG" psql -U baskfy -d "$DB" -tAc "$1" 2>/dev/null | tr -d ' '; }
  HEAD="$(sql 'select version_num from alembic_version')"
  case "$HEAD" in
    "") bad "cannot read alembic_version from $PG/$DB" ;;
    *) [[ "$HEAD" > "0050" ]] && ok "local schema at $HEAD (options tables present)" \
         || bad "local schema $HEAD is older than the options migrations" ;;
  esac
  NEXT="$(sql "select min(expiry_date) from op_expiry where underlying='NIFTY' and expiry_date >= current_date")"
  note "next NIFTY expiry in the local master: ${NEXT:-none (a dev database has no master until refresh-master runs)}"
  note "Kite token: not on the dev stack (the box's desk /status reports it)"
  echo
  [ "$FAILS" -eq 0 ] && echo "OPTIONS OK (local) — every money flag false and pinned, no auto-execute name, schema at head" \
    || { echo "OPTIONS FAIL (local) — $FAILS check(s) failed"; exit 1; }
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
want "GET /api/v1/options/today without a token" 401 "$B/api/v1/options/today"
want "GET desk /nifty-options (basic auth)"       401 "$D/nifty-options"
STATUS="$(crl "$D/status")"
if grep -q '"authed": *true' <<<"$STATUS"; then note "Kite token: authed (desk /status)"
else note "Kite token: no session now (desk /status) — the monitor needs the morning login"; fi

echo "── over SSM"
C='cd /opt/baskfy && docker compose --env-file .env.staging.compose -f compose.prod.yml'
OUT="$(bash "$HERE/box.sh" \
  "echo '## ps'; $C ps --format '{{.Service}} {{.Status}}'" \
  "echo '## beat'; $C exec -T beat python -c 'from baskfy_worker.celery_app import app; print(chr(10).join(sorted(app.conf.beat_schedule)))'" \
  "echo '## desk-env'; $C exec -T desk env | grep -E '^(DRY_RUN|OPTIONS_ENABLED|INTRADAY_ENABLED|BASKFY_OPTIONS_[A-Z0-9_]+)=' | sort" \
  "echo '## monitor-env'; $C exec -T options-monitor env | grep -E '^(DRY_RUN|OPTIONS_ENABLED|INTRADAY_ENABLED|BASKFY_OPTIONS_[A-Z0-9_]+)=' | sort" \
  "echo '## worker-env'; $C exec -T worker env | grep -E '^BASKFY_OPTIONS_[A-Z0-9_]+=' | sort" \
  "echo '## expiry'; $C exec -T postgres psql -U baskfy -d baskfy -tAc \"select min(expiry_date) from op_expiry where underlying='NIFTY' and expiry_date >= current_date\"" \
  "echo '## alembic'; $C exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version'" \
  "echo '## monitor-log'; $C logs --no-log-prefix --tail 5 options-monitor" \
  2>&1)" || bad "box.sh failed:
$OUT"
section() { sed -n "/^## $1\$/,/^## /p" <<<"$OUT" | grep -v '^## '; }
for svc in desk options-monitor worker beat; do
  section ps | grep -qE "^${svc} Up" && ok "$svc Up" || bad "$svc is not Up"
done
for entry in options-contract-master options-collect-chain options-index-bars options-scan \
             options-plan-o1 options-plan-o2 options-plan-o3 options-checks options-weekly; do
  section beat | grep -qx "$entry" && ok "beat schedules $entry" || bad "beat schedule lacks $entry"
done
for svc in desk monitor; do
  ENV="$(section "$svc-env")"
  for f in OPTIONS_ENABLED INTRADAY_ENABLED $MONEY_FLAGS; do
    grep -qx "$f=false" <<<"$ENV" && ok "$svc: $f=false" || bad "$svc: $f is not false"
  done
  grep -qE 'OPTIONS[A-Z0-9_]*AUTO' <<<"$ENV" && bad "$svc: an OPTIONS…AUTO variable is set" \
    || ok "$svc: no OPTIONS…AUTO variable"
  note "$svc: $(grep -m1 '^BASKFY_OPTIONS_MONITOR_ENABLED=' <<<"$ENV" || echo 'BASKFY_OPTIONS_MONITOR_ENABLED unset')"
done
[ "$(section desk-env)" = "$(section monitor-env)" ] && ok "desk and options-monitor agree on every options flag" \
  || bad "desk and options-monitor DISAGREE on the options flags"
for f in $OPS_FLAGS; do note "worker: $(grep -m1 "^$f=" <<<"$(section worker-env)" || echo "$f unset")"; done
NEXT="$(section expiry | tr -d ' ')"
[ -n "$NEXT" ] && ok "next NIFTY expiry in op_expiry: $NEXT" || bad "op_expiry has no future NIFTY expiry (refresh-master has not run)"
note "alembic: $(section alembic)"
note "options-monitor: $(section monitor-log | tail -1)"

echo
[ "$FAILS" -eq 0 ] && echo "OPTIONS OK — $B, $D: every money flag false in desk and monitor, no auto-execute, monitor Up, next expiry ${NEXT:-?}" \
  || { echo "OPTIONS FAIL — $FAILS check(s) failed"; exit 1; }
