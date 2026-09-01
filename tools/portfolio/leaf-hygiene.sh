#!/usr/bin/env bash
# The repo's house rules, applied to one pure-domain leaf. Called by every B-leaf's gates.
#
#   bash tools/portfolio/leaf-hygiene.sh <module_name>     e.g. cash_ledger
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint" || exit 2
MOD="packages/core/src/baskfy_core/${1:?module name}.py"
TEST="packages/core/tests/test_${1}.py"
fail=0

for f in "$MOD" "$TEST"; do
  [ -f "$f" ] || { echo "  MISSING $f"; fail=1; }
done
[ "$fail" -eq 0 ] || { echo "HYGIENE FAILED"; exit 1; }

echo "  rule 3 — no escape hatches"
for pattern in "type: ignore" ": Any\b"; do
  n=$(grep -cE "$pattern" "$MOD" "$TEST" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  printf "     %-16s %s\n" "$pattern" "$n"
  [ "$n" = "0" ] || fail=1
done

echo "  rule 9 — money is Decimal, never float"
n=$(grep -cE "\bfloat\b" "$MOD" || true)
printf "     float in module: %s\n" "$n"
[ "$n" = "0" ] || fail=1

echo "  law 1 — packages/core touches nothing"
n=$(grep -cE "^\s*(import|from)\s+(sqlalchemy|httpx|requests|redis|asyncpg|boto3|os|pathlib)\b" "$MOD" || true)
printf "     I/O imports: %s\n" "$n"
[ "$n" = "0" ] || fail=1
n=$(grep -cE "\b(datetime\.now|dt\.date\.today|time\.time)\s*\(" "$MOD" || true)
printf "     clock reads: %s\n" "$n"
[ "$n" = "0" ] || fail=1

echo "  rule 4 — lint and types"
uv run ruff check "$MOD" "$TEST" >/dev/null 2>&1 && echo "     ruff clean" || { echo "     ruff FAILED"; fail=1; }
uv run ruff format --check "$MOD" "$TEST" >/dev/null 2>&1 && echo "     format clean" || { echo "     format FAILED"; fail=1; }
uv run mypy "$MOD" "$TEST" >/dev/null 2>&1 && echo "     mypy clean" || { echo "     mypy FAILED"; fail=1; }

echo
[ "$fail" -eq 0 ] && echo "HYGIENE OK" || echo "HYGIENE FAILED"
exit "$fail"
