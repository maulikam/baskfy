#!/usr/bin/env bash
# One investments journey: the legacy tree must serve no page of its own, and every legacy path
# must land on its /me twin with the id preserved.
set -uo pipefail
WEB=/Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web
BODIES=$(find "$WEB/src/app/(app)/investments" -name "page.tsx" 2>/dev/null | wc -l | tr -d ' ')
echo "LEGACY_PAGE_BODIES=$BODIES"
FAIL=0
for p in /investments /investments/9 /investments/9/orders /investments/9/customize /investments/9/costs; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "http://127.0.0.1:3000$p")
  dest=$(curl -s -o /dev/null -w "%{redirect_url}" --max-time 20 "http://127.0.0.1:3000$p")
  want="http://127.0.0.1:3000/me${p}"
  echo "REDIRECT $p -> ${dest#http://127.0.0.1:3000}"
  [ "$code" = "308" ] && [ "$dest" = "$want" ] || FAIL=1
done
# grep, not rg: rg is not on PATH in this shell, and a missing binary made this line print
# APP_LINKS_TO_LEGACY=0 from a *failed* command rather than from a measurement.
LINKS=$(cd "$WEB" && grep -rn --include='*.tsx' -E 'href=\{?"?`?/investments' src 2>/dev/null | grep -v '__tests__' | wc -l | tr -d ' ')
echo "APP_LINKS_TO_LEGACY=$LINKS"
[ "$BODIES" = "0" ] && [ "$LINKS" = "0" ] && [ "$FAIL" = "0" ] && echo "ONE_JOURNEY_OK" || echo "ONE_JOURNEY_FAIL"
