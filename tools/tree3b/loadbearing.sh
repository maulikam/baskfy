#!/usr/bin/env bash
# Tree 3b / G3 — the three kept stubs are the ONLY thing making their URL work.
#
# /instruments, /market and /me have no next.config redirect. Their page.tsx `redirect()` call is
# the mechanism. They sit in the authenticated group, so an anonymous request 307s to login with
# the original path preserved in `next` — that is the proof the route resolved rather than 404ing.
set -uo pipefail
BASE="${BASE:-http://localhost:3000}"
fail=0
check() { # path expected-next
  local path="$1" want="$2" code loc
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE$path")
  loc=$(curl -s -D - -o /dev/null --max-time 20 "$BASE$path" 2>/dev/null \
        | grep -i '^location:' | head -1 | tr -d '\r' | sed 's/[Ll]ocation: //')
  if [ "$code" = "307" ] && [ "$loc" = "$want" ]; then
    printf '  %-14s %s -> %s\n' "$path" "$code" "$loc"
  else
    printf '  %-14s UNEXPECTED %s -> %s (wanted 307 -> %s)\n' "$path" "$code" "${loc:-none}" "$want"
    fail=1
  fi
}
echo "plain GET:"
check /instruments "/login?next=%2Finstruments"
check /market      "/login?next=%2Fmarket"
check /me          "/login?next=%2Fme"

echo "RSC / prefetch (the App Router soft-navigation path):"
for p in /instruments /market /me; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
         -H 'RSC: 1' -H 'Next-Router-Prefetch: 1' "$BASE$p")
  # 200 is correct here: the middleware answers a prefetch of a gated route itself.
  case "$code" in
    200|307) printf '  %-14s %s\n' "$p" "$code" ;;
    404)     printf '  %-14s 404 — the stub was deleted and nothing replaced it\n' "$p"; fail=1 ;;
    *)       printf '  %-14s UNEXPECTED %s\n' "$p" "$code"; fail=1 ;;
  esac
done

echo
[ "$fail" -eq 0 ] && echo "LOADBEARING OK — 3 kept stubs resolve on both request paths" \
                  || echo "LOADBEARING FAILED"
exit "$fail"
