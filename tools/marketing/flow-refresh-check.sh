#!/usr/bin/env bash
# Gate G8 for gates/marketing-flow-refresh.md.
#
# Three checks the animation edit could plausibly break, in the order that fails cheapest:
# typecheck, the whole web suite, then eslint against the recorded baseline. Prints FLOW OK only
# when all three hold, so the gate cannot pass on a partial run.
set -uo pipefail

WEB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../decile-blueprint/apps/web" && pwd)"
cd "$WEB" || exit 1

# Pre-existing eslint problems on this tree. Raising it hides a regression; it is a ceiling.
#
# 22 on 26 Aug 2026; re-measured to 24 on 27 Aug 2026. The two new ones are NOT from the flow
# rebuild — they were measured by stashing only its four files (`how-it-works-flow.tsx`, its test,
# `(marketing)/page.tsx`, `globals.css`) and re-running: still 24. They come from other
# uncommitted work in this tree, and every one of them is in a file the rebuild never touched
# (`data-table.tsx`, `results-panel.tsx`, `mark-invested-form.tsx`, and the rest — run
# `pnpm exec eslint .` and read the paths). `pnpm exec eslint` over the rebuild's own four files
# reports nothing at all, which is the claim this ceiling is here to protect.
BASELINE=24

echo "── typecheck"
if ! pnpm exec tsc --noEmit 2>&1 | tail -20; then
  echo "FLOW FAIL: typecheck"; exit 1
fi

echo "── vitest"
SUITE="$(pnpm exec vitest run 2>&1 | tail -40)"
echo "$SUITE" | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
if echo "$SUITE" | grep -qE 'Tests +[0-9]+ failed'; then
  echo "FLOW FAIL: suite red"; exit 1
fi
if ! echo "$SUITE" | grep -qE 'Tests +[0-9]+ passed'; then
  echo "FLOW FAIL: no test summary"; exit 1
fi

echo "── eslint"
LINT="$(pnpm exec eslint . 2>&1 | tail -5)"
echo "$LINT"
COUNT="$(printf '%s' "$LINT" | grep -oE '[0-9]+ problems?' | head -1 | grep -oE '[0-9]+' || echo 0)"
COUNT="${COUNT:-0}"
if [ "$COUNT" -gt "$BASELINE" ]; then
  echo "FLOW FAIL: eslint $COUNT > baseline $BASELINE"; exit 1
fi

echo "FLOW OK (eslint $COUNT <= $BASELINE)"
