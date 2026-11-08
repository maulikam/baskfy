#!/usr/bin/env bash
# AF lane MENU — every navigation destination, probed anonymously against staging.
#
# Sign-in is Google-only behind an allowlist, so no agent can hold a session. What an anonymous
# probe *can* prove is the half that matters most: the route itself compiles and is served. A
# signed-out GET must answer a redirect to /login (an app page) or 200 (a marketing page). A 500
# means the route is broken before any session is involved, and a 404 means the nav points at a
# page that does not exist.
#
# Usage: bash ops/af/menu-probe.sh
# Reads the Caddy gate password from ops/baskfy-staging-gate-password.txt (never echoed).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"
GATE_FILE="$ROOT/ops/baskfy-staging-gate-password.txt"

[[ -f "$GATE_FILE" ]] || { echo "missing $GATE_FILE" >&2; exit 1; }
GATE_PW="$(tr -d '\r\n' < "$GATE_FILE")"

# Every destination in src/lib/nav.ts (PRIMARY_NAV, SECTION_TABS, NAV_GROUPS) plus the four the
# user menu draws outside NAV_GROUPS (/alerts, /api-keys, /admin, /logout) and /login itself.
PATHS=(
  /home
  /market/today /market/mood /market/listings
  /discover /discover/all /discover/search /discover/collections /discover/compare
  /build /build/backtests /create /build/overlap
  /swing /swing/watchlist /swing/market /swing/positions /swing/journal
  /vbt /vbt/book /vbt/backtest
  /twt /twt/backtest
  /portfolio/overview /portfolio/portfolios /portfolio/holdings /portfolio/activity
  /portfolio/watchlist
  /me /profile /brokers /pricing /me/swing
  /performance /holdings /tradebook /regime /reconcile
  /invoices /fees
  /faq /blog /support
  /alerts /api-keys /admin /logout
  /login
)

fails=0
for p in "${PATHS[@]}"; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 40 \
    -u "baskfy:${GATE_PW}" "$B$p")"
  case "$code" in
    200|307|302|303) verdict=ok ;;
    *) verdict=FAIL; fails=$((fails + 1)) ;;
  esac
  printf '%-28s %s  %s\n' "$p" "$code" "$verdict"
done

echo "---"
echo "${#PATHS[@]} destinations, $fails failing"
[[ "$fails" -eq 0 ]]
