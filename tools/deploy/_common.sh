# Shared by the verify-*.sh scripts. Sourced, never executed.
#
# Every one of these builds or runs a real container and asserts against a real response. None of
# them inspects a YAML file and calls that verification: the gates this backs are about whether
# the thing works, and every bug found while writing them (Caddy's directive order, Compose eating
# `$` in a bcrypt hash, Beat's unwritable schedule, a stale image) was invisible to static reading.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BLUE="$ROOT/decile-blueprint"
COMPOSE_FILE="$BLUE/infra/docker/compose.prod.yml"
ENV_COMPOSE="$ROOT/.env.staging.compose"
WEB_IMAGE="${BASKFY_WEB_IMAGE:-baskfy-web:local}"
PY_IMAGE="${BASKFY_PY_IMAGE:-baskfy-py:local}"
PLATFORM="linux/arm64"

fail() { echo "FAIL: $*" >&2; exit 1; }

# The gate password, written by the local bootstrap. Absent on a fresh clone, which is why the
# scripts that need it say so rather than reporting a false pass.
gate_password() {
  local f="${TMPDIR:-/tmp}/baskfy-gate-pw"
  [ -f "$f" ] || fail "no local gate password at $f — run tools/deploy/bootstrap-local.sh first"
  cat "$f"
}

compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_COMPOSE" "$@"; }

# Base URL of the locally-running stack, honouring the port override in the env file.
base_url() {
  local port
  port="$(grep -E '^BASKFY_HTTP_PORT=' "$ENV_COMPOSE" 2>/dev/null | cut -d= -f2)"
  echo "http://localhost:${port:-80}"
}

require_stack() {
  compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -q 'Up' \
    || fail "the staging stack is not running — bash tools/deploy/bootstrap-local.sh"
}
