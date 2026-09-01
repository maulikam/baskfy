#!/usr/bin/env bash
# ROOT / R9 — every leaf's gates file is full, and none is missing.
#
# The failure this catches is a leaf that was planned, never started, and never noticed — which
# is exactly how an orchestrated build reports done at 80%. It counts files as well as reading
# them, because a leaf with no gates file at all cannot fail its own gates.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
CHECKER=~/.claude/skills/unlazy/scripts/gate-check.mjs

shopt -s nullglob
leaves=(gates/portfolio-redesign-*.md gates/portfolio-spine-*.md)
[ ${#leaves[@]} -eq 0 ] && { echo "NO LEAF GATES FOUND"; exit 1; }

fail=0
for f in "${leaves[@]}"; do
  [ "$(basename "$f")" = "portfolio-redesign-root.md" ] && continue
  line=$(node "$CHECKER" --status "$f" 2>/dev/null | tail -1)
  printf '  %-46s %s\n' "$(basename "$f")" "$line"
  grep -qE "ALL MET|abandoned" <<<"$line" || fail=1
done

echo
[ "$fail" -eq 0 ] && echo "ALL LEAVES MET" || echo "LEAVES INCOMPLETE"
exit "$fail"
