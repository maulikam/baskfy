#!/usr/bin/env bash
# Run a local Python file inside a service container on the box, over SSM.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh worker ops/some-script.py
#
# Same reason as `box-sql.sh`: a gate CHECK reaches `sh -c` as one line, and a Python snippet
# nested inside `docker compose exec` inside `box.sh` loses its quotes. The file is base64'd, so
# nothing in it has to survive word splitting, and it is decoded and fed to `python -` on stdin.
#
# The container is the DEPLOYED image, so the script may only import what that image already has.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: box-python.sh <service> <file.py>" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
SERVICE="$1"; FILE="$2"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 2; }
B64="$(base64 < "$FILE" | tr -d '\n')"
bash "$ROOT/tools/deploy/box.sh" \
  "cd /opt/baskfy && echo $B64 | base64 -d | docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T $SERVICE python -" \
  2>/dev/null
