#!/usr/bin/env bash
# Tree 3b / G2 — every pruned path must redirect exactly as it did before the prune.
#
# The baseline was captured from the running server BEFORE any file was deleted. A 404, or a
# destination that moved, is a regression — and this is the only check that can catch it,
# because the deleted files were unreachable and so no test ever exercised them.
set -uo pipefail
BASE="${BASE:-http://localhost:3000}"
FIXTURE="$(dirname "$0")/redirects-expected.txt"
[ -f "$FIXTURE" ] || { echo "PARITY SKIPPED — no baseline at $FIXTURE"; exit 2; }

fail=0 checked=0
while read -r path want_code want_loc; do
  [ -z "$path" ] && continue
  got_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE$path")
  got_loc=$(curl -s -D - -o /dev/null --max-time 20 "$BASE$path" 2>/dev/null \
            | grep -i '^location:' | head -1 | tr -d '\r' | sed 's/[Ll]ocation: //')
  got_loc="${got_loc:-none}"
  checked=$((checked + 1))
  if [ "$got_code" = "$want_code" ] && [ "$got_loc" = "$want_loc" ]; then
    printf '  %-24s %s -> %s\n' "$path" "$got_code" "$got_loc"
  else
    printf '  %-24s CHANGED: was %s -> %s, now %s -> %s\n' \
      "$path" "$want_code" "$want_loc" "$got_code" "$got_loc"
    fail=1
  fi
done < "$FIXTURE"

echo
[ "$fail" -eq 0 ] && echo "PARITY OK — $checked path(s) redirect exactly as before the prune" \
                  || echo "PARITY FAILED — a pruned path changed behaviour"
exit "$fail"
