#!/usr/bin/env bash
# Node 1.1.1 / G8 — the repo's own rules, applied to the files this node adds.
#
# Not a re-run of the whole suite: specifically the rules CLAUDE.md names, checked against these
# two files, so a pass here means "this node did not weaken the codebase" rather than "the tests
# happened to be green".
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint" || exit 2
MOD=packages/core/src/baskfy_core/allocation_ledger.py
TEST=packages/core/tests/test_allocation_ledger.py
fail=0

echo "rule 3 — no type: ignore, no Any, no swallowed exception"
for pattern in "type: ignore" ": Any\b" "except Exception:  # noqa"; do
  n=$(grep -cE "$pattern" "$MOD" "$TEST" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  printf "   %-28s %s\n" "$pattern" "$n"
  [ "$n" = "0" ] || fail=1
done
out=$(uv run pytest packages/core/tests/test_no_escape_hatches.py -p no:randomly 2>&1 | tail -1)
echo "   repo scanner: $out"
grep -qE "[0-9]+ passed" <<<"$out" || fail=1

echo "rule 9 — money is Decimal, never float"
n=$(grep -cE "\bfloat\b" "$MOD" || true)
printf "   float in the module: %s\n" "$n"
[ "$n" = "0" ] || fail=1

echo "rule 4 — lint and types clean on what this node adds"
uv run ruff check "$MOD" "$TEST" >/dev/null 2>&1 && echo "   ruff check clean" || { echo "   ruff FAILED"; fail=1; }
uv run ruff format --check "$MOD" "$TEST" >/dev/null 2>&1 && echo "   ruff format clean" || { echo "   format FAILED"; fail=1; }
uv run mypy "$MOD" "$TEST" >/dev/null 2>&1 && echo "   mypy clean" || { echo "   mypy FAILED"; fail=1; }

echo
[ "$fail" -eq 0 ] && echo "HOUSE RULES OK" || echo "HOUSE RULES FAILED"
exit "$fail"
