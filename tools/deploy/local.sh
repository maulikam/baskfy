#!/usr/bin/env bash
# The whole product on this Mac, read-only, from the bundle exit-aws.sh brought home.
#
#     bash tools/deploy/local.sh restore   # once: volumes + Postgres from the bundle, row counts checked
#     bash tools/deploy/local.sh up        # start (on demand — Maulik, 29 Oct 2026: not always on)
#     bash tools/deploy/local.sh down      # stop; data stays in the named volumes
#     bash tools/deploy/local.sh verify    # every container's money flags read false / DRY_RUN true
#
# READ-ONLY, and not by preference: Kite accepts API orders only from the registered static IP
# (docs/07 §4a), which was the AWS EIP. A home connection is not that IP, so every execution and
# auto-execute flag is forced false here and `up` refuses to finish if any container reads true.
# Screens, bars, the nightly catch-up, holdings and live quotes need no registered IP and work.
#
# Same compose.prod.yml as the box, unmodified, with one override: Docker Desktop cannot bind
# /opt, so the SSH bind comes from the bundle; Caddy serves the app on :8480 and the desk on :8481
# (host-based vhosts need DNS this Mac does not have). Tailscale serves both over HTTPS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCK="$ROOT/decile-blueprint/infra/docker"
BUNDLE="${BASKFY_EXIT_BUNDLE:-/Volumes/T7-Work/baskfy-aws-exit-20261029/bundle}"
HOME_DIR="${BASKFY_LOCAL_DIR:-/Volumes/T7-Work/baskfy-local}"
TAG="${BASKFY_LOCAL_TAG:-d78907f}"
URL="${BASKFY_LOCAL_URL:-http://localhost:8480}"
P="${BASKFY_LOCAL_PROJECT:-baskfy-local}"
SVC="caddy web api worker ingest-worker beat desk desk-daily swing-monitor twt-auto session-supervisor options-monitor fno-monitor"

die() { echo "✗ $*" >&2; exit 1; }
compose() { docker compose -p "$P" --project-directory "$HOME_DIR" -f "$HOME_DIR/compose.prod.yml" \
              -f "$HOME_DIR/compose.local.yml" --env-file "$HOME_DIR/.env.staging.compose" "$@"; }
# Every name that can let an order out. Matched by shape, so a flag added later is caught too.
MONEY='(EXECUTION_ENABLED|AUTO_EXECUTE|AUTO_EXIT|_AUTO|CARRY_ENABLED|INTRADAY_ENABLED|OPTIONS_ENABLED)='

stage() {
  mkdir -p "$HOME_DIR/box"
  [ -f "$HOME_DIR/box/.env.staging" ] || tar -C "$HOME_DIR/box" -xzf "$BUNDLE/config.tgz"
  chmod -R go-rwx "$HOME_DIR/box"
  cp "$DOCK/compose.prod.yml" "$DOCK/Caddyfile" "$HOME_DIR/"
  # The box's env, with every money flag forced off and DRY_RUN forced on. Appended last so it
  # wins over the box's own lines (compose and env_file both take the last assignment).
  for f in .env.staging .env.staging.compose; do
    { grep -Ev "$MONEY|^(DRY_RUN|BASKFY_DRY_RUN|BASKFY_DESK_DRY_RUN|BASKFY_(PY|WEB|DESK)_IMAGE|BASKFY_SITE_ADDRESS|BASKFY_DESK_SITE_ADDRESS|BASKFY_PUBLIC_URL|BASKFY_HTTPS?_PORT|BASKFY_ACME_[A-Z_]*)=" "$HOME_DIR/box/$f" || true
      grep -oE "^[A-Z0-9_]+($MONEY)" "$HOME_DIR/box/$f" | sed 's/=$/=false/' || true
      cat <<EOF
# --- local.sh: read-only on this Mac (no registered static IP) ---
DRY_RUN=true
BASKFY_DRY_RUN=true
BASKFY_DESK_DRY_RUN=true
BASKFY_SWING_EXECUTION_ENABLED=false
BASKFY_SWING_AUTO_EXECUTE=false
BASKFY_TWT_EXECUTION_ENABLED=false
BASKFY_TWT_AUTO_EXECUTE=false
BASKFY_VBT_EXECUTION_ENABLED=false
BASKFY_FNO_F3_AUTO_EXIT=false
BASKFY_PY_IMAGE=baskfy-py:$TAG
BASKFY_WEB_IMAGE=baskfy-web:$TAG
BASKFY_DESK_IMAGE=baskfy-desk:$TAG
BASKFY_SITE_ADDRESS=:80
BASKFY_DESK_SITE_ADDRESS=:8081
BASKFY_PUBLIC_URL=$URL
BASKFY_HTTP_PORT=8480
BASKFY_HTTPS_PORT=8443
EOF
    } > "$HOME_DIR/$f.tmp" && mv "$HOME_DIR/$f.tmp" "$HOME_DIR/$f" && chmod 600 "$HOME_DIR/$f"
  done
  cat > "$HOME_DIR/compose.local.yml" <<EOF
services:
  caddy: {ports: !override ["127.0.0.1:8480:80", "127.0.0.1:8481:8081"]}
  api:           {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  worker:        {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  ingest-worker: {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  beat:          {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  migrate:       {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
  seed:          {volumes: ["$HOME_DIR/box/secrets/ssh:/var/lib/baskfy/.ssh:ro"]}
EOF
  docker image inspect "baskfy-py:$TAG" >/dev/null 2>&1 || \
    gunzip -c "$(dirname "$BUNDLE")/images/baskfy-images-$TAG.tar.gz" | docker load
}

rowcounts() {
  sed -n "/<<'SQL'$/,/^SQL$/p" "$ROOT/tools/deploy/exit-aws.sh" | sed '1d;$d' |
    compose exec -T postgres psql -U baskfy -d baskfy -At | sed 's/|/\t/'
}

verify() {
  local bad=0 c
  for c in $(docker ps -q --filter "label=com.docker.compose.project=$P"); do
    if docker exec "$c" env 2>/dev/null | grep -E "$MONEY" | grep -viE '=(false|0)?$'; then
      echo "  ^ in $(docker inspect -f '{{.Name}}' "$c")"; bad=1; fi
    if docker exec "$c" env 2>/dev/null | grep -E '^DRY_RUN=' | grep -viE '=true$'; then bad=1; fi
  done
  [ "$bad" = 0 ] || { echo "✗ a money flag is not false — stopping the stack" >&2; return 1; }; echo "✓ read-only: every money flag false, DRY_RUN true"
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
      "baskfy-py:$TAG" -xzpf "/b/$(basename "$t")" --numeric-owner -C /v
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
  compose run --rm migrate
  compose up -d $SVC
  verify || { compose stop $SVC; exit 1; }
  echo "app  http://localhost:8480    desk http://localhost:8481"
  echo "remote (after 'tailscale serve' — see docs/LOCAL.md): $URL"
  ;;
stage)   stage ;;
down)    compose stop ;;
verify)  verify ;;
status)  compose ps --format '{{.Service}}\t{{.Status}}' ;;
rowcounts) rowcounts ;;
*) echo "usage: $0 restore|up|down|verify|status|rowcounts" >&2; exit 64 ;;
esac
