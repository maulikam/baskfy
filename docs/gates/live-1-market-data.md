# Gates: LV1 — market-data permission separate from DRY_RUN; the live overlay honest end to end

Scope: `quotes_permitted()` stops reading DRY_RUN (a read-only market-data switch `BASKFY_LIVE_QUOTES` replaces it); every quote carries its exchange time; stale or missing rows can never look live; coverage is counted.

- [x] M1: quotes_permitted() is true with DRY_RUN=true when a real unexpired token exists, and false when BASKFY_LIVE_QUOTES=false
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_live_prices.py -k 'dry_run or live_quotes_flag' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 20 passed, 20 deselected in 0.12s

- [x] M2: no order-capable path reads BASKFY_LIVE_QUOTES (the flag is market data only)
  CHECK: grep -rl 'BASKFY_LIVE_QUOTES' decile-blueprint/packages/execution kite-momentum-rebalancer/app | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] M3: /meta/live-marks answers as_of and stale per quote, served_at, requested and covered counts; a quote older than 120 s is stale
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_meta_live_marks.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 16 passed in 0.12s

- [x] M4: the browser drops the overlay when the last good answer is older than 90 s or the last fetch failed; a stale row is marked; the status line counts coverage
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/live-marks.test.ts src/components/screens/__tests__/live-price.test.tsx 2>&1 | grep -E 'Tests ' | tail -1
  EXPECT: /Tests\s+\d+ passed/
  EVIDENCE: Tests  25 passed (25)

- [x] M5: the OpenAPI document and the TypeScript client are regenerated and in sync (regenerating again changes nothing)
  CHECK: cd decile-blueprint && a=$(shasum packages/api-client/openapi.json packages/api-client/src/generated/schema.ts | shasum); make openapi >/dev/null 2>&1; make client >/dev/null 2>&1; b=$(shasum packages/api-client/openapi.json packages/api-client/src/generated/schema.ts | shasum); [ "$a" = "$b" ] && echo IN SYNC || echo DRIFT
  EXPECT: IN SYNC
  EVIDENCE: IN SYNC

- [x] M6: lint clean on both trees touched (ruff, format, mypy strict; tsc, eslint)
  CHECK: cd decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN

- [x] M7: the working agreement's clock table and the freshness copy say "close, plus live overlay when a Kite session exists" — not "when DRY_RUN is off"
  CHECK: grep -c 'BASKFY_LIVE_QUOTES' CLAUDE.md docs/live/DECISIONS-LV.md | awk -F: '{s+=$2} END {print s}'
  EXPECT: /[1-9]/
  EVIDENCE: 3
