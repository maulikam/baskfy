#!/usr/bin/env bash
# Tree 3b / G7 — verify the two claims BEFORE either gate box is ticked.
#
# `gate-check.mjs` reads these boxes as unmet because `EVIDENCE:` is followed by a newline and
# its ATTR_RE only captures the same line. That is a formatting artifact — but "the evidence is
# there, trust it" is exactly the reasoning this discipline exists to refuse. So the claims are
# re-run against today's source: the tests, and the source conditions they assert.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
WEB=decile-blueprint/apps/web
fail=0

echo "(a) duplicate CSP directive"
if [ -f "$WEB/src/lib/__tests__/csp.test.ts" ]; then
  echo "    csp.test.ts exists"
else
  echo "    MISSING csp.test.ts — the claimed regression test is not there"; fail=1
fi
out=$(cd "$WEB" && pnpm exec vitest run src/lib/__tests__/csp.test.ts 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)" | tail -1)
echo "    ${out:-no vitest summary}"
grep -q "passed" <<<"$out" || fail=1
# The source condition the test guards: exactly one real `default-src` DIRECTIVE. Counted as
# quoted directive strings, not raw matches — middleware.ts also names it in a docstring and in
# the comment that explains why it is not repeated, and counting those would prove nothing.
directives=$(grep -cE '^\s*"default-src ' "$WEB/src/middleware.ts")
echo "    real 'default-src' directives emitted by middleware.ts: $directives (must be 1)"
[ "$directives" = "1" ] || fail=1

echo
echo "(b) repeated assumption notes / duplicate React keys"
hits=$(grep -rn "dict.fromkeys" decile-blueprint/services/api/src/baskfy_api/backtests.py 2>/dev/null | wc -l | tr -d ' ')
echo "    order-preserving dedupe (dict.fromkeys) present in backtests.py: $hits"
[ "$hits" -ge 1 ] || fail=1
# The summary line, wherever it lands — `tail -2 | head -1` picked up the progress dots instead.
out=$(cd decile-blueprint && BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" \
      uv run pytest -k "assumptions_state_each_note_once" -p no:randomly 2>&1 \
      | grep -E "[0-9]+ (passed|failed|error)" | tail -1)
echo "    ${out:-no pytest summary}"
grep -qE "[0-9]+ passed" <<<"$out" || fail=1
grep -qE "[0-9]+ (failed|error)" <<<"$out" && fail=1

echo
[ "$fail" -eq 0 ] && echo "SWEEP CLAIMS VERIFIED — both defects are fixed and the tests that prove it pass today" \
                  || echo "SWEEP CLAIMS FAILED — at least one claim does not hold"
exit "$fail"
