#!/usr/bin/env bash
# Node 1.1.2 / G8 — the repo's rules, applied to the files this node adds or edits.
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint" || exit 2
FILES=(
  services/api/alembic/versions/0021_allocation_ledger.py
  packages/core/src/baskfy_core/models/accounts.py
)
fail=0

echo "rule 3 — no type: ignore, no Any"
for pattern in "type: ignore" ": Any\b"; do
  n=$(grep -cE "$pattern" "${FILES[@]}" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  printf "   %-20s %s\n" "$pattern" "$n"
  [ "$n" = "0" ] || fail=1
done
out=$(uv run pytest packages/core/tests/test_no_escape_hatches.py -p no:randomly 2>&1 | tail -1)
echo "   repo scanner: $out"
grep -qE "[0-9]+ passed" <<<"$out" || fail=1

echo "rule 4 — lint and types clean"
uv run ruff check "${FILES[@]}" >/dev/null 2>&1 && echo "   ruff check clean" || { echo "   ruff FAILED"; fail=1; }
uv run ruff format --check "${FILES[@]}" >/dev/null 2>&1 && echo "   ruff format clean" || { echo "   format FAILED"; fail=1; }
uv run mypy "${FILES[@]}" >/dev/null 2>&1 && echo "   mypy clean" || { echo "   mypy FAILED"; fail=1; }

echo "the migration is reversible and the ORM agrees with it"
uv run python -c "
from baskfy_core.models.accounts import Portfolio, PortfolioHolding
names = {c.name for c in Portfolio.__table__.constraints if c.name}
assert 'uq_portfolio_id_kind' in names, names
assert 'ck_portfolio_portfolio_kind_known' in names, names
cols = set(PortfolioHolding.__table__.columns.keys())
assert 'portfolio_kind' in cols, cols
print('   ORM carries kind, source, portfolio_kind and the composite unique key')
" || fail=1

echo
[ "$fail" -eq 0 ] && echo "HOUSE RULES OK" || echo "HOUSE RULES FAILED"
exit "$fail"
