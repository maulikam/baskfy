#!/usr/bin/env bash
# G6 — both suites green, lint at baseline.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
WEB="$BLUE/apps/web"
BASELINE_LINT=22

cd "$WEB" || exit 1
pnpm exec tsc --noEmit >/dev/null 2>&1 || { pnpm exec tsc --noEmit | tail -10; fail "typecheck"; }

WEB_OUT="$(pnpm exec vitest run 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1)"
echo "  web: $WEB_OUT"
grep -q 'failed' <<<"$WEB_OUT" && fail "web suite red"

# `grep -oE '[0-9]+'` on "23 problems" also matched the "0" further along the summary line and
# returned two numbers, which `[` then rejected as "integer expression expected". Take the count
# off the summary line only, and take the first field of it.
LINT="$(pnpm exec eslint . 2>&1 | grep -oE '[0-9]+ problems?' | head -1 | cut -d' ' -f1)"
LINT="${LINT:-0}"
[ "${LINT:-0}" -le "$BASELINE_LINT" ] || fail "eslint ${LINT} > baseline ${BASELINE_LINT}"

cd "$BLUE" || exit 1
# Capture the whole run, then decide from it. The first version grepped `tail -3` for a
# passed/failed line; pytest's last three lines on a failing run are the FAILED list, so the
# pattern matched nothing, `PY_OUT` was empty, and the gate reported SUITES OK over three real
# failures. A check that cannot fail is worse than no check — it launders a red suite as green.
# Use pytest's EXIT CODE, not its printed summary.
#
# Two versions of this grepped for "N passed": the first read `tail -3`, which on a failing run is
# the FAILED list, so it matched nothing and the gate printed SUITES OK over three real failures.
# The second read the whole output and still found nothing — with `-q` and no failures the summary
# does not always survive capture. Both were parsing prose for a fact the process already states
# unambiguously: 0 means everything passed, 1 means something did not. Grepping was the mistake,
# not the pattern.
PY_RAW="$(uv run pytest services/api/tests packages/core/tests -q 2>&1)"
PY_CODE=$?
PY_OUT="$(grep -aoE '[0-9]+ (passed|failed)[a-z, ]*' <<<"$PY_RAW" | tail -1)"
COUNTED="$(grep -aoc '^\.' <<<"$PY_RAW" || true)"
echo "  py:  exit ${PY_CODE}${PY_OUT:+ — $PY_OUT}"
if [ "$PY_CODE" -ne 0 ]; then
  grep -aE '^FAILED|^ERROR' <<<"$PY_RAW" | head -5
  fail "python suite exited ${PY_CODE}"
fi

echo "SUITES OK — eslint ${LINT} <= ${BASELINE_LINT}"
