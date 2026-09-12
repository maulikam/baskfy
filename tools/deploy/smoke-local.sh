#!/usr/bin/env bash
# SW13 — the prod-shaped stack (compose.prod.yml, unmodified) on this laptop, with local images and
# a throwaway environment, so the desk service is proven to come up beside api/worker/beat before
# an AWS session exists. No secret from .env.staging is read: the compose file is copied into a
# scratch project directory beside a generated `.env.staging` (the BASKFY_ half of .env.example)
# and a generated compose env; one override file replaces the box-only `/opt/baskfy/secrets/ssh`
# bind (Docker Desktop cannot mount /opt) with an empty named volume.
#
#     bash tools/deploy/smoke-local.sh              # builds py + desk (cached), reuses baskfy-web:local
#
# Sequence, the same as deploy-swing.sh: migrate → seed reference → seed swing (sleeve + risk) →
# up → curl the api (/api/v1/swing/setups 401, /healthz 200) and the desk through Caddy's
# desk.localhost vhost (/status 200 dry_run true, /swing 401 then 200 with the password, / 200) →
# the running containers' env (DRY_RUN=true, every BASKFY_SWING_* false) → swing-monitor idling →
# `SMOKE OK`, then `down -v`. Project `baskfy-smoke`, host ports 8480/8444 (BASKFY_SMOKE_HTTP_PORT /
# BASKFY_SMOKE_HTTPS_PORT), nothing on 5433/6380. KEEP=1 leaves the stack up for a look.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BLUE="$ROOT/decile-blueprint"
DOCK="$BLUE/infra/docker"
# The agent session's scratchpad when it exists (never the repo, never /tmp's shared root
# for a file that holds a generated password), else a private directory under $TMPDIR.
_AGENT_SCRATCH=/private/tmp/claude-501/-Users-maulikdave-Documents-projects-baskfy/b145c25e-ef2d-4ffe-ae88-9465ecb011fc/scratchpad
SCRATCH="${BASKFY_SMOKE_DIR:-$([ -d "$_AGENT_SCRATCH" ] && echo "$_AGENT_SCRATCH" || echo "${TMPDIR:-/tmp}/baskfy-$(id -u)")}/smoke"
PROJECT=baskfy-smoke
HTTP="${BASKFY_SMOKE_HTTP_PORT:-8480}"
HTTPS="${BASKFY_SMOKE_HTTPS_PORT:-8444}"
PY_IMAGE="${BASKFY_PY_IMAGE:-baskfy-py:local}"
WEB_IMAGE="${BASKFY_WEB_IMAGE:-baskfy-web:local}"
DESK_IMAGE="${BASKFY_DESK_IMAGE:-baskfy-desk:local}"
rnd() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
DESK_PW="smoke-$(rnd 6)"

say()  { printf '\n── %s\n' "$*"; }
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
compose() { docker compose -p "$PROJECT" --project-directory "$SCRATCH" \
              -f "$SCRATCH/compose.prod.yml" -f "$SCRATCH/compose.smoke.yml" \
              --env-file "$SCRATCH/.env.compose" "$@"; }
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: stack left up — teardown: docker compose -p $PROJECT down -v"; return; fi
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "images"
docker build -q -f "$DOCK/Dockerfile.python" -t "$PY_IMAGE" "$BLUE" >/dev/null
docker build -q -f "$DOCK/Dockerfile.desk"   -t "$DESK_IMAGE" "$ROOT" >/dev/null
docker image inspect "$WEB_IMAGE" >/dev/null 2>&1 || {
  echo "   building $WEB_IMAGE (slow, once)"
  # `NEXT_PUBLIC_DESK_URL` is required, not optional: the Dockerfile's default is empty and
  # `src/lib/site.ts` throws in production rather than falling back to the live desk. Without it
  # `next build` fails. A local smoke has no desk, so this points at the loopback console port.
  docker build -q -f "$DOCK/Dockerfile.web" -t "$WEB_IMAGE" \
    --build-arg "NEXT_PUBLIC_SITE_URL=http://localhost:$HTTP" \
    --build-arg "NEXT_PUBLIC_API_ORIGIN=http://localhost:$HTTP" \
    --build-arg "NEXT_PUBLIC_API_URL=http://localhost:$HTTP/api/v1" \
    --build-arg "NEXT_PUBLIC_DESK_URL=http://localhost:$HTTP/desk" \
    --build-arg "BASKFY_RELEASE=smoke" "$BLUE" >/dev/null
}
echo "   $PY_IMAGE  $DESK_IMAGE  $WEB_IMAGE"

say "throwaway project directory: $SCRATCH"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
cp "$DOCK/compose.prod.yml" "$DOCK/Caddyfile" "$SCRATCH/"
# The BASKFY_ half of .env.example, inline comments stripped, secrets left empty. Everything the
# smoke needs to be non-empty comes from the compose env below.
sed -n '/^# THE DATA PLANT/,/^# THE EXECUTION DESK/p' "$ROOT/.env.example" \
  | grep -E '^[A-Z_]+=' | sed -E 's/[[:space:]]+#.*$//' > "$SCRATCH/.env.staging"
grep -q '^BASKFY_SOLE_USER_ID=' "$SCRATCH/.env.staging" || echo 'BASKFY_SOLE_USER_ID=1' >> "$SCRATCH/.env.staging"
cat > "$SCRATCH/.env.compose" <<EOF
BASKFY_DB_PASSWORD=smoke-$(rnd 8)
BASKFY_JWT_SECRET=$(rnd 32)
REVALIDATE_SECRET=$(rnd 32)
BASKFY_GATE_USER=baskfy
BASKFY_GATE_PASSWORD_HASH=unused-since-M46.4
BASKFY_SITE_ADDRESS=:80
BASKFY_PUBLIC_URL=http://localhost:$HTTP
BASKFY_HTTP_PORT=$HTTP
BASKFY_HTTPS_PORT=$HTTPS
BASKFY_DESK_HOST=desk.localhost
BASKFY_DESK_SITE_ADDRESS=http://desk.localhost
BASKFY_DESK_PASSWORD=$DESK_PW
BASKFY_PY_IMAGE=$PY_IMAGE
BASKFY_WEB_IMAGE=$WEB_IMAGE
BASKFY_DESK_IMAGE=$DESK_IMAGE
EOF
cat > "$SCRATCH/compose.smoke.yml" <<'EOF'
# Smoke-only override: the box's host bind for the desk-session SSH key does not exist on a
# laptop. Same destination, so compose replaces the bind with this empty named volume.
services:
  api:           {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
  worker:        {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
  ingest-worker: {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
  beat:          {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
  migrate:       {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
  seed:          {volumes: [smoke-ssh:/var/lib/baskfy/.ssh:ro]}
volumes:
  smoke-ssh: {}
EOF
compose config -q || fail "compose config"
compose down -v --remove-orphans >/dev/null 2>&1 || true

say "migrate"
compose run --rm -T migrate 2>&1 | tail -2
VER="$(compose exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version')"
[ -n "$VER" ] || fail "no alembic_version"
echo "   alembic at $VER"

say "seed reference; an account; seed swing --capital 2500000 --risk 0.5"
compose run --rm -T seed 2>&1 | tail -3
compose exec -T postgres psql -U baskfy -d baskfy -q -c \
  "insert into app_user (public_id, email) values ('smoke', 'smoke@example.test') on conflict do nothing"
compose run --rm -T seed python -m baskfy_api.seed swing --capital 2500000 --risk 0.5 2>&1 | tail -2
SLEEVE="$(compose exec -T postgres psql -U baskfy -d baskfy -tAc 'select sleeve_capital_inr, risk_per_trade_pct, updated_by from sw_config')"
[ "$SLEEVE" = "2500000.00|0.500|seed" ] || fail "sw_config is '$SLEEVE', wanted 2500000.00|0.500|seed"
echo "   sw_config: $SLEEVE"

say "up"
compose up -d api worker ingest-worker beat desk swing-monitor web caddy 2>&1 | grep -E "Error|error" && fail "up" || true
for i in $(seq 1 60); do
  H="$(compose ps --format '{{.Service}} {{.Health}}' | grep -E '^(api|desk) ' | awk '{print $2}' | sort -u | tr '\n' ' ')"
  [ "$H" = "healthy " ] && break
  sleep 3
done
compose ps --format '  {{.Service}} {{.Status}}'
[ "$H" = "healthy " ] || { compose logs --tail 40 desk api; fail "api/desk not healthy: '$H'"; }

say "over Caddy (:$HTTP) — the api and the desk vhost"
B="http://localhost:$HTTP"; D="http://desk.localhost:$HTTP"; R="desk.localhost:$HTTP:127.0.0.1"
c() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 --resolve "$R" "$@"; }
want() { local got; got="$(c "${@:3}")"; [ "$got" = "$2" ] && echo "   $1 → $got" || fail "$1 → $got (wanted $2)"; }
want "GET /api/v1/swing/setups (no token)"    401 "$B/api/v1/swing/setups"
want "GET /api/v1/swing/journal (no token)"   401 "$B/api/v1/swing/journal"
want "GET /healthz"                           200 "$B/healthz"
want "GET /api/v1/meta/universes"             200 "$B/api/v1/meta/universes"
want "GET desk /status (no password)"         200 "$D/status"
curl -s --resolve "$R" "$D/status" | grep -q '"dry_run": *true' || fail "desk /status lacks dry_run true"
echo "   desk /status: dry_run true"
want "GET desk /swing (no password)"          401 "$D/swing"
want "GET desk /swing (password)"             200 -u "x:$DESK_PW" "$D/swing"
want "GET desk / (password)"                  200 -u "x:$DESK_PW" "$D/"
want "GET desk /static/app.css (password)"    200 -u "x:$DESK_PW" "$D/static/app.css"
# Caddy only ever forwards the desk's own name; the desk's Host check (websec.py, 421) is the
# second lock and is exercised from inside the network, where nothing sits in front of it.
FOREIGN="$(compose exec -T desk curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' http://localhost:8420/status)"
[ "$FOREIGN" = "421" ] && echo "   desk /status under a foreign Host (direct) → 421" || fail "desk answered a foreign Host with $FOREIGN, not 421"
curl -sI --resolve "$R" "$D/status" | tr -d '\r' | grep -qi '^x-robots-tag:.*noindex' || fail "no noindex on the desk vhost"
echo "   noindex on the desk vhost"
SKIP_BOX=1 BASKFY_PUBLIC_URL="$B" BASKFY_DESK_URL="$D" BASKFY_CURL_RESOLVE="$R" \
  bash "$ROOT/tools/deploy/verify-swing.sh" | tail -1

say "the rails inside the containers"
for svc in desk swing-monitor; do
  E="$(compose exec -T "$svc" env | grep -E '^(DRY_RUN|BASKFY_SWING_[A-Z_]+|KITE_API_SECRET)=' | sort | tr '\n' ' ')"
  echo "   $svc: $E"
  grep -q 'DRY_RUN=true ' <<<"$E" || fail "$svc: DRY_RUN not true"
  for f in EXECUTION_ENABLED MONITOR_ENABLED EP_PREMARKET_ENABLED TIMING_PROBE; do
    grep -q "BASKFY_SWING_$f=false " <<<"$E" || fail "$svc: BASKFY_SWING_$f not false"
  done
  grep -q 'KITE_API_SECRET= ' <<<"$E" || fail "$svc: KITE_API_SECRET is not blank"
done
compose exec -T desk sh -c 'touch /var/lib/baskfy/state/x 2>/dev/null' && fail "the desk can write the token volume" \
  || echo "   desk: token volume is read-only"
compose logs --no-log-prefix swing-monitor 2>/dev/null | grep -q 'swing-monitor: flag off; next run' \
  || fail "swing-monitor is not idling with the flag off"
echo "   swing-monitor: $(compose logs --no-log-prefix swing-monitor 2>/dev/null | tail -1)"
N="$(compose exec -T postgres psql -U baskfy -d baskfy -tAc "select count(*) from information_schema.tables where table_schema='desk'")"
[ "${N:-0}" -gt 0 ] || fail "the desk schema has no tables"
echo "   desk schema: $N tables (the desk migrated itself into Postgres)"

echo
echo "SMOKE OK — alembic $VER, sw_config $SLEEVE, api 401-gated, desk up at $D behind basic auth, DRY_RUN true, every swing flag false"
