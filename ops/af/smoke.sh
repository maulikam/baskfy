#!/usr/bin/env bash
# AF staging smoke — lane B. Exit non-zero on any miss.
# Usage: bash ops/af/smoke.sh
# Reads the gate password from ops/baskfy-staging-gate-password.txt (never echoed).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"
GATE_FILE="$ROOT/ops/baskfy-staging-gate-password.txt"

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

[[ -f "$GATE_FILE" ]] || fail "missing $GATE_FILE"
GATE_PW="$(tr -d '\r\n' < "$GATE_FILE")"
[[ -n "$GATE_PW" ]] || fail "gate password file is empty"

# Basic-auth for the Caddy gate only — never print GATE_PW.
curl_auth() {
  curl -sS --max-time 40 -u "baskfy:${GATE_PW}" "$@"
}
curl_auth_code() {
  curl -sS -o /dev/null -w '%{http_code}' --max-time 40 -u "baskfy:${GATE_PW}" "$@"
}
curl_auth_headers() {
  curl -sSI --max-time 40 -u "baskfy:${GATE_PW}" "$@"
}

# 1. Anonymous API (no app session) → 401 for previously open book/order routes.
for path in \
  desk/performance \
  baskets \
  baskets/plan \
  explore/broad-market-sharpe/kite
do
  code="$(curl_auth_code "$B/api/v1/$path")"
  [[ "$code" == "401" ]] || fail "GET /api/v1/$path → $code (want 401)"
done

# 2. CSP carries the Kite form target and a frame-src directive.
CSP="$(curl_auth_headers "$B/" | tr -d '\r' | awk -F': ' 'tolower($1)=="content-security-policy"{print substr($0, index($0,$2))}' | head -1)"
[[ -n "$CSP" ]] || fail "no Content-Security-Policy header on /"
grep -q "kite.zerodha.com" <<<"$CSP" || fail "CSP missing kite.zerodha.com"
grep -q "frame-src" <<<"$CSP" || fail "CSP missing frame-src"

# 3. Pipeline health — degraded must be false after the data plant is healthy.
STATUS="$(curl_auth "$B/api/v1/meta/status")"
grep -Eq '"degraded"[[:space:]]*:[[:space:]]*false' <<<"$STATUS" \
  || fail "/api/v1/meta/status degraded is not false: $STATUS"

# 4. Unknown URL is shelled — body still has the app nav (not Next's bare 404).
NOT_FOUND="$(curl_auth "$B/this-does-not-exist")"
grep -Eqi 'role="navigation"|aria-label="[Mm]ain|[Nn]av"|href="/discover"|href="/home"' <<<"$NOT_FOUND" \
  || fail "/this-does-not-exist body does not contain the nav"

# 5. Constituents expose weight_pct as a percent string (house rule 8 / AUDIT 0.3).
#    Authenticated route — optional bearer via BASKFY_SMOKE_BEARER.
CONSTITUENTS_URL="$B/api/v1/explore/broad-market-sharpe/constituents"
if [[ -n "${BASKFY_SMOKE_BEARER:-}" ]]; then
  CONST_BODY="$(curl_auth -H "Authorization: Bearer ${BASKFY_SMOKE_BEARER}" "$CONSTITUENTS_URL")"
else
  CONST_BODY="$(curl_auth "$CONSTITUENTS_URL")"
fi
grep -Eq '"weight_pct"[[:space:]]*:[[:space:]]*"5\.00"' <<<"$CONST_BODY" \
  || fail "constituents API missing weight_pct=5.00 (set BASKFY_SMOKE_BEARER if the route is authed): ${CONST_BODY:0:240}"

echo "SMOKE OK — $B"
