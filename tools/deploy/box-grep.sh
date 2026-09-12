#!/usr/bin/env bash
# Count files inside a container on the box that contain a fixed string.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/box-grep.sh web /app "No session has been read"
#
# WHY. Reaching a file inside a container from a gate CHECK means nesting box.sh -> docker compose
# exec -> sh -lc -> grep "...". A CHECK arrives at `sh -c` as one line, so the innermost quotes
# collapse and the command prints NOTHING — which reads as a broken box rather than a broken quote.
# It happened three times in one session (gates/backfill-compression.md G7,
# gates/deploy-pc-command-center.md D4-D7, gates/twt-empty-page.md E7).
#
# The needle travels as base64 and is written to a file on the box, so `grep -F -f` reads it from
# disk and no quote of the caller's has to survive any layer. Works for strings with spaces,
# quotes and slashes.
set -euo pipefail
[ $# -ge 3 ] || { echo "usage: box-grep.sh <service> <path> <fixed-string>" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
SERVICE="$1"; SEARCH_PATH="$2"; NEEDLE="$3"
B64="$(printf '%s' "$NEEDLE" | base64 | tr -d '\n')"
COMPOSE='docker compose -f compose.prod.yml --env-file .env.staging.compose'
bash "$ROOT/tools/deploy/box.sh" \
  "cd /opt/baskfy && echo $B64 | base64 -d > /tmp/.needle && $COMPOSE cp /tmp/.needle $SERVICE:/tmp/.needle >/dev/null 2>&1 && $COMPOSE exec -T $SERVICE sh -lc 'grep -rlF -f /tmp/.needle $SEARCH_PATH 2>/dev/null | wc -l'" \
  2>/dev/null | tr -d '[:space:]'
echo
