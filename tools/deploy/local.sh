#!/usr/bin/env bash
# The whole product on this Mac, read-only, from the bundle exit-aws.sh brought home.
#
#     bash tools/deploy/local.sh restore   # once: volumes + Postgres from the bundle, row counts checked
#     bash tools/deploy/local.sh up        # start (on demand — Maulik, 29 Oct 2026: not always on)
#     bash tools/deploy/local.sh down      # stop; data stays in the named volumes
#     bash tools/deploy/local.sh verify    # every money flag false; reports DRY_RUN per service
#     bash tools/deploy/local.sh live|dry  # DRY_RUN false|true on every service ($HOME_DIR/dry_run)
#     bash tools/deploy/local.sh pipeline [tag]  # redeploy worker/ingest-worker/beat only (pipeline.tag)
#
# LIVE BY DEFAULT since 1 Nov 2026 (Maulik: "I want DRY_RUN off from entire system, i want to
# take real trades"; DECISIONS-MERGE DRY-LIVE): a confirmed /execute reaches Zerodha. Orders go
# out via the static IP 82.118.26.6 (BASKFY_KITE_PROXY_URL) registered on the Kite app (LOCAL-2).
# Every execution and auto-execute flag is still forced false here and `up` refuses to finish if
# any container reads true; flipping one is a separate decision of Maulik's.
#
# Same compose.prod.yml as the box, unmodified, with one override: Docker Desktop cannot bind
# /opt, so the SSH bind comes from the bundle; Caddy serves the app on :8480 with the desk at
# /desk on the same port (one address, Maulik, 29 Oct 2026). Tailscale serves it over HTTPS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCK="$ROOT/deploy"
BUNDLE="${BASKFY_EXIT_BUNDLE:-/Volumes/T7-Work/baskfy-aws-exit-20261029/bundle}"
HOME_DIR="${BASKFY_LOCAL_DIR:-/Volumes/T7-Work/baskfy-local}"
TAG="${BASKFY_LOCAL_TAG:-$(cat "$HOME_DIR/py.tag" 2>/dev/null || echo d78907f)}"
# The web image is rebuilt here (its NEXT_PUBLIC_* origins are baked at build time, and the box's
# named staging.baskfy.com); `web.tag` holds the local build's tag, else the box's.
WEB_TAG="${BASKFY_LOCAL_WEB_TAG:-$(cat "$HOME_DIR/web.tag" 2>/dev/null || echo "$TAG")}"
# The desk likewise, once it gained the /desk prefix layer (core/path_prefix.py): `desk.tag`.
DESK_TAG="${BASKFY_LOCAL_DESK_TAG:-$(cat "$HOME_DIR/desk.tag" 2>/dev/null || echo "$TAG")}"
# Sign-in without Google (Maulik, 29 Oct 2026): the one account this Mac signs in as.
SIGNIN_ID="${BASKFY_LOCAL_SIGNIN_PUBLIC_ID:-$(cat "$HOME_DIR/signin.public_id" 2>/dev/null || true)}"
# The public URL (NEXTAUTH_URL, the Kite callback's origin). `tailscale` writes it to public.url
# once, so a plain `up` keeps using it; BASKFY_LOCAL_URL still overrides for one run.
URL="${BASKFY_LOCAL_URL:-$(cat "$HOME_DIR/public.url" 2>/dev/null || echo http://localhost:8480)}"
P="${BASKFY_LOCAL_PROJECT:-baskfy-local}"
DRY="$(cat "$HOME_DIR/dry_run" 2>/dev/null || echo false)"; [ "$DRY" = true ] || DRY=false
SVC="caddy web api worker ingest-worker beat desk desk-daily swing-monitor twt-auto session-supervisor options-monitor fno-monitor"
RS_TAG="${BASKFY_LOCAL_RS_TAG:-$(cat "$HOME_DIR/rs.tag" 2>/dev/null || echo local)}"
RS_SVC="api worker ingest-worker beat desk desk-daily session-supervisor swing-monitor twt-auto fno-monitor options-monitor"
# The data pipeline is deployed apart from the app (Maulik, 7 Nov 2026): its own image tag
# (pipeline.tag), recreated only by `local.sh pipeline [tag]`; `up` never restarts it.
PIPE_SVC="worker ingest-worker beat"
APP_SVC=""; for _s in $SVC; do [[ " $PIPE_SVC " == *" $_s "* ]] || APP_SVC="$APP_SVC $_s"; done
PIPE_TAG="${BASKFY_LOCAL_PIPELINE_TAG:-$(cat "$HOME_DIR/pipeline.tag" 2>/dev/null || echo "$RS_TAG")}"

die() { echo "✗ $*" >&2; exit 1; }
compose() {
  local f=(-f "$HOME_DIR/compose.prod.yml" -f "$HOME_DIR/compose.local.yml")
  [ -s "$HOME_DIR/compose.rust.yml" ] && f+=(-f "$HOME_DIR/compose.rust.yml")
  # Backup resolvers for the pipeline services (9 Nov 2026). Reverse: delete the file, `local.sh pipeline`.
  [ -s "$HOME_DIR/compose.dns.yml" ] && f+=(-f "$HOME_DIR/compose.dns.yml")
  docker compose -p "$P" --project-directory "$HOME_DIR" "${f[@]}" --env-file "$HOME_DIR/.env.staging.compose" "$@"
}
# Refuses to recreate the stack while a nightly chain holds its advisory lock
# (orchestrator PIPELINE_CHAIN_LOCK_KEY 0x0BA5C1FA): a SIGKILLed worker leaves the run unpublished
# and rolled back (7 Nov 2026). A killed run frees the lock, so a stale `running` row never blocks.
# BASKFY_LOCAL_FORCE=1 overrides.
pipeline_guard() {
  [ "${BASKFY_LOCAL_FORCE:-0}" = 1 ] && return 0
  local pg held; pg="$(compose ps -q postgres 2>/dev/null)"
  [ -n "$pg" ] || return 0
  held="$(docker exec "$pg" psql -U baskfy -d baskfy -At -c \
    "select 'pid '||l.pid||' since '||to_char(a.xact_start,'HH24:MI') from pg_locks l
       join pg_stat_activity a using (pid)
     where l.locktype='advisory' and l.classid=0 and l.objid=195412474 and l.objsubid=1 and l.granted" 2>/dev/null || true)"
  [ -z "$held" ] && return 0
  die "a nightly pipeline is running ($held); wait for it or BASKFY_LOCAL_FORCE=1"
}
# Every name that can let an order out. Matched by shape, so a flag added later is caught too.
MONEY='(EXECUTION_ENABLED|AUTO_EXECUTE|AUTO_EXIT|_AUTO|CARRY_ENABLED|INTRADAY_ENABLED|OPTIONS_ENABLED)='

# Execution/auto flags Maulik turned on for this Mac (8 Nov 2026), one name per line in money.on.
# Only these six may appear; code defaults, intraday and options stay false.
MONEY_ON_ALLOWED=" BASKFY_SWING_EXECUTION_ENABLED BASKFY_SWING_AUTO_EXECUTE BASKFY_TWT_EXECUTION_ENABLED BASKFY_TWT_AUTO_EXECUTE BASKFY_VBT_EXECUTION_ENABLED BASKFY_FNO_F3_AUTO_EXIT "
money_on() {
  local n; [ -s "$HOME_DIR/money.on" ] || return 0
  for n in $(cat "$HOME_DIR/money.on"); do
    [[ "$MONEY_ON_ALLOWED" == *" $n "* ]] || die "money.on: $n may not be turned on here"
    echo "$n"
  done
}

stage() {
  mkdir -p "$HOME_DIR/box"
  [ -s "$HOME_DIR/pipeline.tag" ] || echo "$PIPE_TAG" > "$HOME_DIR/pipeline.tag"   # pin on first use
  [ -f "$HOME_DIR/box/.env.staging" ] || tar -C "$HOME_DIR/box" -xzf "$BUNDLE/config.tgz"
  chmod -R go-rwx "$HOME_DIR/box"
  cp "$DOCK/compose.prod.yml" "$DOCK/Caddyfile" "$HOME_DIR/"
  # The box's env, with every money flag forced off and DRY_RUN set to $DRY. Appended last so it
  # wins over the box's own lines (compose and env_file both take the last assignment).
  for f in .env.staging .env.staging.compose; do
    { grep -Ev "$MONEY|^(DRY_RUN|BASKFY_DRY_RUN|BASKFY_DESK_DRY_RUN|BASKFY_(PY|WEB|DESK|RS)_IMAGE|BASKFY_SITE_ADDRESS|BASKFY_DESK_SITE_ADDRESS|BASKFY_PUBLIC_URL|BASKFY_DESK_HOST|BASKFY_LOCAL_SIGNIN_PUBLIC_ID|DESK_PATH_PREFIX|BASKFY_HTTPS?_PORT|BASKFY_ACME_[A-Z_]*)=" "$HOME_DIR/box/$f" || true
      grep -oE "^[A-Z0-9_]+($MONEY)" "$HOME_DIR/box/$f" | sed 's/=$/=false/' || true
      cat <<EOF
# --- local.sh: DRY_RUN=$DRY (DRY-LIVE); every execution/auto flag forced false ---
DRY_RUN=$DRY
BASKFY_DRY_RUN=$DRY
BASKFY_DESK_DRY_RUN=$DRY
BASKFY_SWING_EXECUTION_ENABLED=false
BASKFY_SWING_AUTO_EXECUTE=false
BASKFY_TWT_EXECUTION_ENABLED=false
BASKFY_TWT_AUTO_EXECUTE=false
BASKFY_VBT_EXECUTION_ENABLED=false
BASKFY_FNO_F3_AUTO_EXIT=false
BASKFY_SITE_ADDRESS=:80
BASKFY_DESK_SITE_ADDRESS=:8081
BASKFY_PUBLIC_URL=$URL
BASKFY_DESK_HOST=$(echo "$URL" | sed -E 's|^[a-z]+://||; s|[:/].*$||')
BASKFY_HTTP_PORT=8480
BASKFY_HTTPS_PORT=8443
BASKFY_LOCAL_SIGNIN_PUBLIC_ID=$SIGNIN_ID
DESK_PATH_PREFIX=/desk
# Opportunities Ask panel: local Ollama on the Mac (empty URL = keyword matching)
BASKFY_LLM_URL=http://host.docker.internal:11434/v1
BASKFY_LLM_MODEL=qwen3:8b
BASKFY_LLM_TIMEOUT_S=20
EOF
      for _n in $(money_on); do echo "$_n=true"; done
      # Image tags only interpolate compose; kept out of the containers' env_file so a new app
      # image does not change the pipeline services' config and recreate them.
      if [ "$f" = .env.staging.compose ]; then printf '%s\n' "BASKFY_WEB_IMAGE=baskfy-web:$WEB_TAG" "BASKFY_DESK_IMAGE=baskfy-desk:$DESK_TAG" \
        "BASKFY_RS_IMAGE=baskfy-rs:$RS_TAG" "BASKFY_RS_PIPELINE_IMAGE=baskfy-rs:$PIPE_TAG"; fi
    } > "$HOME_DIR/$f.tmp" && mv "$HOME_DIR/$f.tmp" "$HOME_DIR/$f" && chmod 600 "$HOME_DIR/$f"
  done
  # The API reads the JWT secret from .env.staging (env_file); the web signs with the one compose
  # interpolates from .env.staging.compose. The box shipped them different, which refuses every
  # local sign-in (30 Oct 2026). Compared by hash, never printed.
  jwt_hash() { grep '^BASKFY_JWT_SECRET=' "$HOME_DIR/$1" | tail -1 | cut -d= -f2- | tr -d '\n' | shasum -a 256; }
  [ "$(jwt_hash .env.staging)" = "$(jwt_hash .env.staging.compose)" ] || \
    die "BASKFY_JWT_SECRET differs between .env.staging and .env.staging.compose; make them equal"
  cat > "$HOME_DIR/compose.local.yml" <<EOF
services:
  # One address (Maulik, 29 Oct 2026): the desk is /desk on the app's port; its own vhost is not published.
  caddy: {ports: !override ["127.0.0.1:8480:80"]}
  # The web service takes no env_file, so the local sign-in account is named here, for it alone.
  web:           {environment: {BASKFY_LOCAL_SIGNIN_PUBLIC_ID: "\${BASKFY_LOCAL_SIGNIN_PUBLIC_ID:-}"}}
  # LOCAL-1 (30 Oct 2026): the API is always DRY_RUN=false, so the Kite login exchange and the
  # holdings/trades reads work. The API has no order path.
  api:           {environment: {DRY_RUN: "false"}, volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  # LOCAL-3 (2 Nov 2026, Maulik): no HTTP Basic on the local desk. compose.prod.yml still
  # interpolates BASKFY_DESK_PASSWORD (the box copy stays in the env file); this blanks the
  # process that serves /desk. AWS/prod is unchanged.
  desk:          {environment: {DESK_PASSWORD: "", BASKFY_DESK_WEB_UI: "true"}}  # DESK-UI: browser page GETs go to the web app
  worker:        {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  ingest-worker: {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  beat:          {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  migrate:       {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
EOF
  render_rust
}

# The Rust override, cut down to the services named in rust.services (none: no override at all).
render_rust() {
  local on; on=" $(tr '\n' ' ' 2>/dev/null < "$HOME_DIR/rust.services" || true) "
  if [ -z "${on// /}" ]; then rm -f "$HOME_DIR/compose.rust.yml"; return 0; fi
  awk -v on="$on" '/^services:/ {svc=1; print; next} !svc {print; next}
    /^  [a-z][a-z0-9-]*:/ {n=$1; sub(/:$/, "", n); keep=(n == "migrate" || index(on, " " n " ") > 0)} keep {print}' \
    "$ROOT/tools/deploy/compose.rust.yml" > "$HOME_DIR/compose.rust.yml"
}

# The named services (default: every Rust-capable one), each checked against RS_SVC.
pick() {
  local s; [ "$#" -gt 0 ] || set -- $RS_SVC
  for s in "$@"; do case " $RS_SVC " in *" $s "*) echo "$s" ;; *) die "no Rust build of '$s' (one of: $RS_SVC)" ;; esac; done
}

# Moves the named services between runtimes: rust.services is the state, stage renders it.
switch() {
  local to="$1"; shift
  local want; want="$(pick "$@" | tr '\n' ' ')"
  mkdir -p "$HOME_DIR"; touch "$HOME_DIR/rust.services"
  if [ "$to" = rust ]; then
    docker image inspect "baskfy-rs:$RS_TAG" >/dev/null 2>&1 || die "no image baskfy-rs:$RS_TAG — build Dockerfile.rust first"
    { cat "$HOME_DIR/rust.services"; printf '%s\n' $want; } | sort -u | grep . > "$HOME_DIR/rust.services.tmp" || true
  else
    grep -vxF -f <(printf '%s\n' $want) "$HOME_DIR/rust.services" > "$HOME_DIR/rust.services.tmp" || true
  fi
  mv "$HOME_DIR/rust.services.tmp" "$HOME_DIR/rust.services"
  case " $want " in *" worker "*|*" ingest-worker "*|*" beat "*) pipeline_guard ;; esac
  stage
  compose up -d --no-deps $want
  if ! verify; then
    echo "✗ verify failed: stopping $want" >&2
    compose stop $want; exit 1
  fi
  echo "on Rust: $(tr '\n' ' ' < "$HOME_DIR/rust.services")"
}

# New submissions only; cancels, GTT changes and reconciliation carry on.
desk_post() {
  compose exec -T desk curl -sS --fail-with-body -m 10 -X POST -H 'Origin: http://desk' \
    -H 'Content-Type: application/json' -d "$2" "http://desk:8420$1"; echo
}

# The image's newest migration against the database: the runner starts only when they differ.
schema_step() {
  local want have
  want="$(docker run --rm --entrypoint sh "baskfy-rs:$RS_TAG" -c 'ls /usr/share/baskfy/migrations' \
    | sed -nE 's/^0*([0-9]+)_.*\.sql$/\1/p' | sort -n | tail -1)"
  [ -n "$want" ] || die "baskfy-rs:$RS_TAG ships no migrations: rebuild it from deploy/Dockerfile.rust"
  have() { compose exec -T postgres psql -U baskfy -d baskfy -Atc \
    "select coalesce(max(version),0) from _sqlx_migrations where success" 2>/dev/null || echo none; }
  have="$(have)"
  if [ "$have" = "$want" ]; then echo "✓ schema at $want: no migration to run"; return 0; fi
  echo "schema at ${have:-none}, image wants $want: running migrate"
  compose run --rm migrate
  have="$(have)"
  [ "$have" = "$want" ] || die "schema at $have after migrate, image wants $want"
}

# Each Rust service's health through the compose network (its own DNS name), or its run state.
rust_check() {
  local bad=0 s id url st
  [ -s "$HOME_DIR/rust.services" ] || { echo "no service is on Rust"; return 0; }
  for s in $(cat "$HOME_DIR/rust.services"); do
    id="$(compose ps -q "$s")"
    [ -n "$id" ] || { echo "✗ $s: not running"; bad=1; continue; }
    [ "$(docker inspect -f '{{index .Config.Labels "baskfy.runtime"}}' "$id")" = rust ] || { echo "✗ $s: not on baskfy-rs"; bad=1; continue; }
    st="$(docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' "$id")"
    case "$s" in api) url="http://api:8000/health" ;; desk) url="http://desk:8420/status" ;; *) url= ;; esac
    if [ -n "$url" ]; then
      if compose exec -T "$s" curl -fsS -m 10 -o /dev/null "$url"; then echo "✓ $s $url ($st)"; else echo "✗ $s $url ($st)"; bad=1; fi
    else
      case "$st" in "running restarts=0") echo "✓ $s ($st)" ;; *) echo "✗ $s ($st)"; bad=1 ;; esac
    fi
  done
  return "$bad"
}

rowcounts() {
  sed -n "/<<'SQL'$/,/^SQL$/p" "$ROOT/tools/deploy/retired/exit-aws.sh" | sed '1d;$d' |
    compose exec -T postgres psql -U baskfy -d baskfy -At | sed 's/|/\t/'
}

# Fails on any execution/auto flag not false, or on a container live while dry_run says true.
# DRY_RUN=false is the chosen state (DRY-LIVE), so it is reported loudly, not failed.
verify() {
  local bad=0 c svc d live=""
  local on; on="$(money_on | paste -sd '|' -)"; on="${on:-__none__}"
  for c in $(docker ps -q --filter "label=com.docker.compose.project=$P"); do
    svc="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c")"
    if docker exec "$c" env 2>/dev/null | grep -E "$MONEY" | grep -viE '=(false|0)?$' | grep -vE "^($on)=true$"; then
      echo "  ^ in $svc"; bad=1; fi
    d="$(docker exec "$c" env 2>/dev/null | grep -E '^DRY_RUN=' | tail -1 | cut -d= -f2- || true)"
    [ -n "$d" ] || continue
    [ "$svc" = api ] && continue   # LOCAL-1: always false, no order path
    if [ "$(echo "$d" | tr 'A-Z' 'a-z')" = false ]; then
      live="$live $svc"
      [ "$DRY" = true ] && { echo "  ^ DRY_RUN=false in $svc but dry_run says true"; bad=1; }
    fi
  done
  [ "$bad" = 0 ] || { echo "✗ a money flag is not false, or a service is live while dry_run=true" >&2; return 1; }
  if [ "$on" = __none__ ]; then echo "✓ every execution/auto flag false"
  else echo "⚠ execution/auto flags ON (money.on): ${on//|/ }; every other money flag false"; fi
  if [ -n "$live" ]; then
    printf '\033[1;31m⚠ LIVE: DRY_RUN=false in%s — a confirmed /execute places REAL orders on Zerodha\033[0m\n' "$live"
  else echo "✓ DRY_RUN=true on every order-capable service (api: false, LOCAL-1)"; fi
}

case "${1:-}" in
restore)
  [ -f "$BUNDLE/SHA256SUMS" ] || die "no bundle at $BUNDLE — run exit-aws.sh fetch first"
  (cd "$BUNDLE" && shasum -a 256 -c --quiet SHA256SUMS) || die "bundle checksum mismatch"
  stage
  compose ps -q postgres | grep -q . && die "$P already has a postgres; restore runs once, into empty volumes"
  # Every volume the box had (exit-aws.sh packs them all but pgdata, which is the dump).
  for t in "$BUNDLE"/vol-baskfy-staging_*.tgz; do
    v="${P}_$(basename "$t" .tgz | sed 's/^vol-baskfy-staging_//')"
    docker volume create "$v" >/dev/null
    docker run --rm -v "$v:/v" -v "$BUNDLE:/b:ro" --user 0 --entrypoint tar \
      "baskfy-rs:$RS_TAG" -xzpf "/b/$(basename "$t")" --numeric-owner -C /v
    echo "  volume $v"
  done
  compose up -d --wait postgres redis
  compose exec -T postgres psql -U baskfy -d baskfy -qc \
    "create extension if not exists timescaledb version '$(cat "$BUNDLE/timescale.version")'; select timescaledb_pre_restore();" >/dev/null
  docker cp "$BUNDLE/baskfy.dump" "$(compose ps -q postgres):/tmp/baskfy.dump"
  compose exec -T postgres sh -c 'pg_restore -U baskfy -d baskfy /tmp/baskfy.dump 2>/tmp/restore.err; rm /tmp/baskfy.dump' || true
  compose exec -T postgres psql -U baskfy -d baskfy -qc "select timescaledb_post_restore(); analyze;" >/dev/null
  echo "pg_restore notes (an existing timescaledb extension is expected):"
  compose exec -T postgres sh -c 'grep -E "error|ERROR" /tmp/restore.err | sort | uniq -c | head -20' || true
  rowcounts > "$HOME_DIR/rowcounts.local.tsv"
  if diff <(sort "$BUNDLE/rowcounts.tsv") <(sort "$HOME_DIR/rowcounts.local.tsv"); then
    echo "✓ RESTORE OK — $(wc -l < "$BUNDLE/rowcounts.tsv" | tr -d ' ') tables, $(awk -F'\t' '{s+=$2} END {print s}' "$BUNDLE/rowcounts.tsv") rows, identical to the box"
  else die "row counts differ from the box (diff above) — do NOT tear down AWS"; fi
  ;;
up)
  stage
  compose up -d --wait postgres redis
  schema_step
  compose up -d --no-deps $APP_SVC
  compose up -d --no-deps --no-recreate $PIPE_SVC   # started if absent, never restarted
  verify || { echo "✗ stopping the stack" >&2; compose stop $SVC; exit 1; }
  echo "app  http://localhost:8480    desk http://localhost:8480/desk"
  echo "remote (after 'tailscale serve' — see docs/LOCAL.md): $URL"
  ;;
tailscale)
  # Maulik, 29 Oct 2026: reach it from the phone over Tailscale, never the public internet.
  # `serve` proxies the tailnet's HTTPS to the 127.0.0.1 ports; Caddy stays bound to localhost.
  TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  [ -x "$TS" ] || die "Tailscale is not installed: brew install --cask tailscale-app"
  NAME="$("$TS" status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  [ -n "$NAME" ] || die "Tailscale is not signed in: open the Tailscale app and log in"
  "$TS" serve --bg --https=443 http://127.0.0.1:8480 || die "serve refused — enable HTTPS certificates: https://login.tailscale.com/admin/dns"
  "$TS" serve --https=8443 off >/dev/null 2>&1 || true   # the desk lives at /desk now
  echo "https://$NAME" > "$HOME_DIR/public.url"
  echo "app  https://$NAME"; echo "desk https://$NAME/desk"
  NOW=$(TZ=Asia/Kolkata date +%H%M)
  if [ "$NOW" -ge 1840 ] && [ "$NOW" -le 2120 ]; then
    echo "nightly window: not restarting now — run 'local.sh up' after 21:20 IST so the app uses this URL"
  else exec bash "$0" up; fi
  ;;
pipeline)
  # The only path that restarts the pipeline services; refuses while a chain is running.
  [ -n "${2:-}" ] && { docker image inspect "baskfy-rs:$2" >/dev/null 2>&1 || die "no image baskfy-rs:$2"; PIPE_TAG="$2"; }
  pipeline_guard
  echo "$PIPE_TAG" > "$HOME_DIR/pipeline.tag"
  stage
  compose up -d --no-deps $PIPE_SVC
  verify
  echo "pipeline on baskfy-rs:$PIPE_TAG (app on baskfy-rs:$RS_TAG)"
  ;;
stage)   stage ;;
down)    pipeline_guard; compose stop ;;
verify)  verify ;;
live|dry)
  # Recreates every service so all read the new DRY_RUN; execution/auto flags stay false.
  pipeline_guard
  [ "$1" = live ] && DRY=false || DRY=true
  echo "$DRY" > "$HOME_DIR/dry_run"
  stage
  compose up -d $SVC
  verify
  ;;
status)  compose ps --format '{{.Service}}\t{{.Status}}' ;;
rowcounts) rowcounts ;;
rust)    shift; switch rust "$@" ;;
python)  die "Python no longer runs here: every service is on Rust" ;;
rust-check) rust_check ;;
ready)   compose exec -T desk curl -sS -m 10 http://desk:8420/execution/ready; echo ;;
pause)   desk_post /execution/pause "{\"reason\":\"${2:-operator}\"}" ;;
resume)  desk_post /execution/resume '{}' ;;
*) echo "usage: $0 restore|up|down|verify|live|dry|pipeline [tag]|status|rowcounts|tailscale|rust [svc...]|python [svc...]|rust-check|ready|pause [reason]|resume" >&2; exit 64 ;;
esac
