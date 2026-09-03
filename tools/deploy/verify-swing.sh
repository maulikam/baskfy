#!/usr/bin/env bash
# SW13 — is the merged desk actually up on the box, and are the rails still on? Two halves:
#
#   over HTTPS from this laptop (no token, no password):
#     /api/v1/swing/setups answers 401 (auth-gated, alive); /api/v1/swing/journal the same shape;
#     /healthz 200 and /api/v1/meta/universes 200 (Caddy → api); https://desk.<host>/status 200
#     with "dry_run": true; https://desk.<host>/swing and / answer 401 (the desk's basic auth —
#     never 5xx, never 421 which would mean DESK_ALLOWED_HOSTS lacks the vhost); noindex on both.
#   over SSM (box.sh):
#     beat's schedule lists every swing entry; the desk and swing-monitor containers' env shows
#     DRY_RUN=true and all four BASKFY_SWING_* false; the token volume is mounted read-only in the
#     desk; alembic is at head; every service is Up.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/verify-swing.sh
# Override the hosts with BASKFY_PUBLIC_URL / BASKFY_DESK_URL; SKIP_BOX=1 runs the HTTPS half only;
# BASKFY_CURL_RESOLVE=desk.localhost:8480:127.0.0.1 pins a name for the local smoke.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/tools/deploy"
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"
D="${BASKFY_DESK_URL:-https://desk.${B#https://}}"
FAILS=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; FAILS=$((FAILS+1)); }
# An operating choice the operator made: shown, never asserted. A flag the run must never see
# true has `bad` behind it instead; this one exists so a verify reads like a state of the world.
note() { printf '  --   %s\n' "$*"; }
RESOLVE="${BASKFY_CURL_RESOLVE:-}"
crl()  { curl -s --max-time 40 ${RESOLVE:+--resolve "$RESOLVE"} "$@"; }
code() { crl -o /dev/null -w '%{http_code}' "$@"; }
want() { # want <label> <expected-regex> <url...>
  local label="$1" exp="$2"; shift 2
  local got; got="$(code "$@")"
  [[ "$got" =~ ^($exp)$ ]] && ok "$label → $got" || bad "$label → $got (wanted $exp)"
}

echo "── over HTTPS: $B and $D"
want "GET /api/v1/swing/setups without a token"   401     "$B/api/v1/swing/setups"
want "GET /api/v1/swing/journal without a token"  401     "$B/api/v1/swing/journal"
want "GET /healthz (Caddy)"                       200     "$B/healthz"
want "GET /api/v1/meta/universes (Caddy → api)"   200     "$B/api/v1/meta/universes"
want "GET desk /status (no password by design)"   200     "$D/status"
STATUS="$(crl "$D/status")"
grep -q '"dry_run": *true' <<<"$STATUS" && ok "desk /status reports dry_run true" \
  || bad "desk /status does not report dry_run true: ${STATUS:0:120}"
# SW21: the desk's Kite reads take a slot from the box's Redis clock, not from a per-container
# spacer. "per-process" here is not an outage — the desk degrades to its own spacers and keeps
# trading — but it means the web container and swing-monitor are two limits again, which is the
# state SW20.1 wrote down and SW21 closed. Asserted, because it is wiring, not an operating choice.
grep -q '"read_limiter": *"shared"' <<<"$STATUS" && ok "desk /status reports read_limiter shared" \
  || bad "desk read limiter is not shared: $(grep -o '"read_limiter":[^,}]*' <<<"$STATUS" || echo "no read_limiter in /status")"
want "GET desk /swing (basic auth, not 421/5xx)"  401     "$D/swing"
want "GET desk / (basic auth)"                    401     "$D/"
want "GET desk /static/app.css through Caddy"     401     "$D/static/app.css"
for u in "$B/" "$D/status"; do
  crl -I "$u" | tr -d '\r' | grep -qi '^x-robots-tag:.*noindex' \
    && ok "noindex on $u" || bad "no noindex on $u"
done
crl -I "$D/status" | tr -d '\r' | grep -qi '^strict-transport-security:' \
  && ok "HSTS on the desk vhost" || bad "no HSTS on the desk vhost"

if [ "${SKIP_BOX:-0}" != "1" ]; then
  echo "── over SSM"
  C='cd /opt/baskfy && docker compose --env-file .env.staging.compose -f compose.prod.yml'
  OUT="$(bash "$HERE/box.sh" \
    "echo '## ps'; $C ps --format '{{.Service}} {{.Status}}'" \
    "echo '## beat'; $C exec -T beat python -c 'from baskfy_worker.celery_app import app; print(chr(10).join(sorted(k for k in app.conf.beat_schedule if k.startswith(\"swing\"))))'" \
    "echo '## desk-env'; $C exec -T desk env | grep -E '^(DRY_RUN|BASKFY_REDIS_URL|BASKFY_SWING_[A-Z_]+)=' | sort" \
    "echo '## monitor-env'; $C exec -T swing-monitor env | grep -E '^(DRY_RUN|BASKFY_REDIS_URL|BASKFY_SWING_[A-Z_]+)=' | sort" \
    "echo '## desk-mounts'; docker inspect --format '{{range .Mounts}}{{.Destination}} rw={{.RW}}{{\"\\n\"}}{{end}}' \$($C ps -q desk)" \
    "echo '## alembic'; $C exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version'; $C run --rm --no-deps migrate alembic heads 2>/dev/null | tail -1" \
    "echo '## desk-schema'; $C exec -T postgres psql -U baskfy -d baskfy -tAc \"select count(*) from information_schema.tables where table_schema='desk'\"" \
    2>&1)" || bad "box.sh failed:
$OUT"
  section() { sed -n "/^## $1\$/,/^## /p" <<<"$OUT" | grep -v '^## '; }

  for svc in caddy web api worker ingest-worker beat desk swing-monitor postgres redis; do
    section ps | grep -qE "^${svc} Up" && ok "$svc Up" || bad "$svc is not Up"
  done
  for entry in swing-eod swing-eod-plan swing-weekend swing-premarket-levels swing-premarket-gaps; do
    section beat | grep -qx "$entry" && ok "beat schedules $entry" || bad "beat schedule lacks $entry"
  done
  for svc in desk monitor; do
    ENV="$(section "$svc-env")"
    grep -qx 'DRY_RUN=true' <<<"$ENV" && ok "$svc: DRY_RUN=true" || bad "$svc: DRY_RUN is not true"
    # Two of the four are RAILS and are asserted: EXECUTION_ENABLED is the line between a
    # SIMULATED confirm and money, and TIMING_PROBE spends a Kite call on a morning nobody asked
    # about. The other two are OPERATING CHOICES — Maulik turned the monitor and the pre-open
    # scan on for 4 Sep — so they are reported, not asserted; a deploy must not quietly turn off
    # what the operator turned on, and a verify must not fail because he did.
    for flag in EXECUTION_ENABLED TIMING_PROBE; do
      grep -qx "BASKFY_SWING_$flag=false" <<<"$ENV" && ok "$svc: BASKFY_SWING_$flag=false" \
        || bad "$svc: BASKFY_SWING_$flag is not false"
    done
    for flag in MONITOR_ENABLED EP_PREMARKET_ENABLED; do
      note "$svc: $(grep -m1 "BASKFY_SWING_$flag=" <<<"$ENV" || echo "BASKFY_SWING_$flag unset")"
    done
    # SW21: both processes must point at the same Redis, or "shared" above is one container's
    # word for a limit the other one is not taking. The monitor has no HTTP surface to ask.
    grep -q '^BASKFY_REDIS_URL=redis://' <<<"$ENV" && ok "$svc: BASKFY_REDIS_URL is set" \
      || bad "$svc: BASKFY_REDIS_URL is unset — its Kite reads are limited per process"
  done
  section desk-mounts | grep -q '^/var/lib/baskfy/state rw=false' \
    && ok "desk mounts the token volume read-only" || bad "the desk can write the token volume"
  HEAD="$(section alembic | sed -n 2p | awk '{print $1}')"; NOW="$(section alembic | sed -n 1p)"
  [ -n "$NOW" ] && [ "$NOW" = "${HEAD:-$NOW}" ] && ok "alembic at $NOW (head ${HEAD:-?})" \
    || bad "alembic version_num=$NOW head=$HEAD"
  N="$(section desk-schema | tr -d ' ')"
  [ "${N:-0}" -gt 0 ] 2>/dev/null && ok "desk schema has $N tables" || bad "desk schema is empty"
fi

echo
[ "$FAILS" -eq 0 ] && echo "SWING OK — $B, $D: auth-gated API, desk up behind basic auth, DRY_RUN true, execution and probe flags false (the monitor and pre-open flags read above as the operator set them)" \
  || { echo "SWING FAIL — $FAILS check(s) failed"; exit 1; }
