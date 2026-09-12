#!/usr/bin/env bash
# Post-deploy verification for the Portfolio Command Center (12 Sep 2026).
#
# Why a script and not four CHECK lines: each of these runs a command on the box through
# `box.sh`, which runs it through SSM, and two of them run a further command inside the `web`
# container. That is three levels of quoting, and a gate CHECK is handed to `sh -c` as a single
# line — the nested quotes collapse and the command produces no output at all, which reads as a
# failed deploy when it is a failed quote. `gates/deploy-pc-command-center.md` D4–D7 call this.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh <tag>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="${1:-$(cd "$ROOT" && git rev-parse --short HEAD)}"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
box() { bash "$ROOT/tools/deploy/box.sh" "$@" 2>/dev/null; }

COMPOSE='docker compose -f compose.prod.yml --env-file .env.staging.compose'

pins=$(box "cd /opt/baskfy && grep -cE '^BASKFY_(WEB|PY|DESK)_IMAGE=.*:${TAG}\$' .env.staging.compose" | tr -d '[:space:]')
running=$(box "cd /opt/baskfy && $COMPOSE ps --format '{{.Service}}={{.State}}' | grep -c '=running'" | tr -d '[:space:]')
cmd=$(box "cd /opt/baskfy && $COMPOSE exec -T web sh -lc 'grep -rl \"Portfolio Command Center\" /app | wc -l'" | tr -d '[:space:]')
release=$(box "cd /opt/baskfy && $COMPOSE exec -T web sh -lc 'echo \$BASKFY_RELEASE'" | tr -d '[:space:]')
twt=$(box "cd /opt/baskfy && grep -ciE '^BASKFY_TWT_EXECUTION_ENABLED=true' .env.staging.compose || true" | tr -d '[:space:]')

printf 'pins=%s running=%s command_center=%s release=%s twt_execution_true=%s\n' \
  "${pins:-0}" "${running:-0}" "${cmd:-0}" "${release:-none}" "${twt:-0}"
