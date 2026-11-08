#!/usr/bin/env bash
# D1 / G4 — every route §2 asks for resolves. A 307 to /login is correct for a gated route;
# a 404 means the route does not exist and the nav points at nothing.
set -uo pipefail
BASE="${BASE:-http://localhost:3000}"
fail=0
for p in /portfolio/overview /portfolio/portfolios /portfolio/holdings /portfolio/activity /portfolio/watchlist; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 "$BASE$p" 2>/dev/null)
  printf '  %-26s %s\n' "$p" "$code"
  case "$code" in 200|307|308) ;; *) fail=1 ;; esac
done
echo
[ "$fail" -eq 0 ] && echo "ROUTES OK" || echo "ROUTES FAILED — a nav destination 404s"
exit "$fail"
