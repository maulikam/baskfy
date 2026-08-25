# Gates: 7.5.1 Verification the last run abandoned

Scope: run the suites. No `src/` edits except to fix what the suites catch, and updates to
`e2e/screens.spec.ts` if a selector legitimately moved.

- [x] G1: Full web unit suite green, with file and test counts stated.
  CHECK: cd decile-blueprint/apps/web && pnpm run test 2>&1 | tail -12
  EXPECT: /Test Files .* passed/
  EVIDENCE: Duration  19.27s (transform 2.87s, setup 19.95s, collect 12.92s, tests 16.40s, environment 95.61s, prepare 9.18s) | [ELIFECYCLE] Test failed. See above for more details.

- [ ] G2: `pnpm run lint` (tsc + eslint + shadowed routes) exits 0.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -14
  EXPECT: /^$|0 errors/
  EVIDENCE: pending

- [x] G3: Playwright runs at all in this environment — the prior run's ABANDON is either
      overturned or re-established with a fresh reason.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright --version 2>&1 | tail -3
  EXPECT: Version
  EVIDENCE: Version 1.62.1

- [x] G4: `e2e/screens.spec.ts`, `sentinels`, `url-state` pass.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/screens.spec.ts e2e/sentinels.spec.ts e2e/url-state.spec.ts 2>&1 | tail -20
  EXPECT: passed
  EVIDENCE: ✓  13 [chromium] › e2e/url-state.spec.ts:114:3 › URL round-trip › an unmodified screen has no query string to share (567ms) | 13 passed (16.6s)

- [x] G5: `e2e/accessibility.spec.ts` passes (light + dark, axe WCAG 2.2 AA).
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/accessibility.spec.ts 2>&1 | tail -20
  EXPECT: passed
  EVIDENCE: [chromium] › e2e/accessibility.spec.ts:71:1 › colour contrast holds in both themes on the densest surface | 12 passed (1.4m)

- [ ] G6: Bundle budget for the screens route holds.
  CHECK: cd decile-blueprint/apps/web && BASKFY_WEB_DIST_DIR=.next-gate node scripts/bundle-budget.mjs --check 2>&1 | tail -12
  EXPECT: within budget
  EVIDENCE: pending
