# Gates: SB7 — user-defined cash sleeve on create basket

Scope: Cash % is suggested (5% or exposure tier), investor overrides; 0% allowed; preview matches save.

- [x] G1: materialize allows 0–95% without flooring at 5%
  CHECK: cd decile-blueprint/apps/web && ./node_modules/.bin/vitest run src/lib/basket/__tests__/sizing.test.ts src/lib/basket/__tests__/materialize.test.ts -t "cash|zero" 2>&1 | tail -5
  EXPECT: passed
  EVIDENCE: Start at  20:32:07 | Duration  1.01s (transform 73ms, setup 272ms, collect 106ms, tests 4ms, environment 1.08s, prepare 92ms)

- [x] G2: SizingControls exposes cash % with reset-to-suggested
  CHECK: cd decile-blueprint/apps/web && ./node_modules/.bin/vitest run src/components/create/__tests__/create-from-screen.test.tsx -t "cash" 2>&1 | tail -5
  EXPECT: passed
  EVIDENCE: Start at  20:32:08 | Duration  1.20s (transform 151ms, setup 71ms, collect 314ms, tests 264ms, environment 317ms, prepare 43ms)

- [ ] G3: API accepts explicit cash_pct on from-screen save
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_curated_from_screen.py -k cash -q 2>&1 | tail -5
  EXPECT: passed
  EVIDENCE: pending

- [ ] G4: Core parity test lists ZERO_CASH_PCT
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_holding_profile_parity.py packages/core/tests/test_basket_sizing.py -k "zero or cash" -q 2>&1 | tail -5
  EXPECT: passed
  EVIDENCE: pending
