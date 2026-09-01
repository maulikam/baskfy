#!/usr/bin/env bash
# D6 / G5 — the wiring did not break the tree, and added no lint debt.
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint/apps/web" || exit 2
BASELINE_ESLINT="${BASELINE_ESLINT:-22}"
fail=0

echo "tsc --noEmit"
pnpm exec tsc --noEmit >/dev/null 2>&1 && echo "  clean" || { echo "  FAILED"; fail=1; }

echo "the files this leaf owns are eslint-clean"
owned=(
  src/lib/portfolio/create.ts
  src/lib/portfolio/draft-mapping.ts
  src/app/actions/portfolio.ts
  src/components/portfolio/new-portfolio-flow.tsx
  src/components/portfolio/unallocated-section.tsx
  "src/app/(app)/portfolio/holdings/page.tsx"
  src/lib/portfolio/__tests__/create.test.ts
  src/components/portfolio/__tests__/confirm-wiring.test.tsx
)
pnpm exec eslint "${owned[@]}" >/dev/null 2>&1 && echo "  clean" || { echo "  FAILED"; fail=1; }

echo "repo-wide eslint is no worse than the baseline"
count=$(pnpm exec eslint . 2>&1 | sed -n 's/.*✖ [0-9]* problems (\([0-9]*\) errors.*/\1/p' | head -1)
[ -z "$count" ] && count=0
echo "  $count errors (baseline $BASELINE_ESLINT)"
[ "$count" -le "$BASELINE_ESLINT" ] || fail=1

echo "the whole web suite"
out=$(pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1)
echo "  $out"
grep -q "failed" <<<"$out" && fail=1
grep -qE "Tests +[0-9]+ passed" <<<"$out" || fail=1

echo
[ "$fail" -eq 0 ] && echo "WIRING OK" || echo "WIRING FAILED"
exit "$fail"
