#!/usr/bin/env bash
# Every file this tree edited: eslint + tsc.
set -uo pipefail
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web || exit 1
FILES="src/lib/marketing/sample-screen.ts src/components/integrations/api-key-manager.tsx \
src/app/(app)/me/investments/page.tsx src/app/(app)/me/investments/[id]/page.tsx \
src/app/(app)/me/investments/[id]/orders/page.tsx src/app/(app)/me/investments/[id]/customize/page.tsx \
src/app/(app)/me/investments/[id]/costs/page.tsx"
FAIL=0
pnpm exec eslint $FILES 2>&1 | tail -5
pnpm exec eslint $FILES >/dev/null 2>&1 || { echo "ESLINT_FAIL"; FAIL=1; }
pnpm exec tsc --noEmit 2>&1 | grep -E "error TS" | head -5
pnpm exec tsc --noEmit >/dev/null 2>&1 || { echo "TSC_FAIL"; FAIL=1; }
[ "$FAIL" = "0" ] && echo "TREE4_LINT_OK" || echo "TREE4_LINT_NOT_OK"
