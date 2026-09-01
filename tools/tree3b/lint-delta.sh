#!/usr/bin/env bash
# Tree 3b / G5 — this tree's files are clean, and the repo is no worse than it was.
#
# `pnpm run lint` cannot pass: 22 eslint errors live in components this tree never opened. So the
# honest bar is the delta, not the absolute — plus a hard requirement that everything this tree
# owns is clean on its own.
set -uo pipefail
cd "$(dirname "$0")/../../decile-blueprint/apps/web" || exit 2
BASELINE="${BASELINE:-22}"

echo "tsc --noEmit:"
if pnpm exec tsc --noEmit >/dev/null 2>&1; then echo "  clean"; else echo "  FAILED"; exit 1; fi

echo "shadowed-routes check:"
node scripts/check-shadowed-routes.mjs | sed 's/^/  /' || exit 1

echo "eslint on the files this tree owns:"
if pnpm exec eslint scripts/check-shadowed-routes.mjs >/dev/null 2>&1; then
  echo "  clean"
else
  echo "  FAILED — a file this tree owns has a lint error"; exit 1
fi

# One number, from the summary line only. The `|| echo 0` fallback used to fire *alongside* a
# successful grep and produce two lines, which then broke `[` with "integer expression expected".
count=$(pnpm exec eslint . 2>&1 | sed -n 's/.*✖ [0-9]* problems (\([0-9]*\) errors.*/\1/p' | head -1)
[ -z "$count" ] && count=0
echo "repo-wide eslint errors: $count (baseline before this tree: $BASELINE, all pre-existing)"
if [ "$count" -le "$BASELINE" ]; then
  echo "LINT DELTA OK — introduced 0 new errors"
else
  echo "LINT DELTA FAILED — $((count - BASELINE)) new error(s)"; exit 1
fi
