# Gates: leaf-6.3 — Double-render + jargon sweep

Scope: Exactly one SEBI disclaimer per app page from AppShell; remove desk jargon from consumer UI.

- [x] G1: portfolios-list does not import Disclaimer
  CHECK: rg -n "Disclaimer" decile-blueprint/apps/web/src/components/portfolios/portfolios-list.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G2: backtests-list does not import Disclaimer
  CHECK: rg -n "Disclaimer" decile-blueprint/apps/web/src/components/backtests/backtests-list.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G2b: backtest-result (detail) does not duplicate Disclaimer — AppShell owns it
  CHECK: rg -n "import \{ Disclaimer \}" decile-blueprint/apps/web/src/components/backtests/backtest-result.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G3: screens-list and screen-editor do not duplicate Disclaimer
  CHECK: rg -n "import \{ Disclaimer \}" decile-blueprint/apps/web/src/components/screens/ || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G4: Featured basket page has no MomentumScan / desk / run-id user-facing strings
  CHECK: rg -n "MomentumScan|for the desk|screen_run_id|data version" decile-blueprint/apps/web/src/app/\(app\)/baskets/featured/page.tsx || echo CLEAN
  EXPECT: CLEAN
  EVIDENCE: CLEAN

- [x] G5: jargon-ban vitest (includes backtest-result Disclaimer guard)
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/jargon-ban.test.ts 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: 4 tests passed
