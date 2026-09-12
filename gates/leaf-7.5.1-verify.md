# Gates: 7.5.1 Verification the last run abandoned

Scope: run the suites. No `src/` edits except to fix what the suites catch, and updates to
`e2e/screens.spec.ts` if a selector legitimately moved.

- [x] G1: Full web unit suite green, with file and test counts stated.
  CHECK: cd decile-blueprint/apps/web && pnpm run test 2>&1 | tail -12
  EXPECT: /Test Files +[0-9]+ passed \([0-9]+\)/
  EVIDENCE: Start at  01:55:19 | Duration  31.60s (transform 2.22s, setup 11.04s, collect 9.74s, tests 30.18s, environment 53.01s, prepare 5.65s)

- [x] G2: `pnpm run lint` (tsc + eslint + shadowed routes) exits 0.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -14
  EXPECT: /^$|0 errors/
  EVIDENCE: 0 errors, 1 warning (12 Sep 2026). The warning is the pre-existing react-hooks/incompatible-library note on data-table.tsx. It was red until today on two errors in this tree's own uncommitted tests — a TS2322 in filter-chip-bar.test.tsx and a require-await in default-view.test.tsx — which had been failing every lint gate in the repository.

- [x] G3: Playwright runs at all in this environment — the prior run's ABANDON is either
      overturned or re-established with a fresh reason.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright --version 2>&1 | tail -3
  EXPECT: Version
  EVIDENCE: Version 1.62.1

- [ ] G4: `e2e/screens.spec.ts`, `sentinels`, `url-state` pass.
      ⏳ **Still unrun, and honestly so.** Two attempts on 12 Sep 2026 did not produce a result: the
      first was killed by `rerun.py --timeout 600` while Playwright was still starting its two
      servers (the config allows 300s each for the API and for `next build && next start`, so 600s
      cannot cover them), and its orphaned run then held port 3100 and stalled the second. Both were
      killed to clear the port. The servers themselves were healthy — API `/health` 200 and web 200 —
      so nothing here suggests the specs fail; they simply have not been observed to pass.
      Needs one uninterrupted run with a timeout above ~900s and nothing else building.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/screens.spec.ts e2e/sentinels.spec.ts e2e/url-state.spec.ts 2>&1 | tail -20
  EXPECT: passed
  EVIDENCE: pending

- [x] G5: `e2e/accessibility.spec.ts` passes (light + dark, axe WCAG 2.2 AA).
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/accessibility.spec.ts 2>&1 | tail -20
  EXPECT: passed
  EVIDENCE: [chromium] › e2e/accessibility.spec.ts:49:5 › landing has no accessibility violations in dark ── | 14 passed (24.3s)

- [x] G6: Bundle budget for the screens route holds.
      🔴 **Genuinely red, and it had been invisible.** 12 Sep 2026: the check reported nothing
      because `scripts/bundle-budget.mjs` still named the pre-rename routes — `screens`,
      `dashboard`, `market-health`, `backtests` — and five of its six routes read
      "— not in manifest —". Only `instruments/[symbol]` kept its path, which is why the script
      still looked like it ran. **So the 250 KB budget docs/11 sets had not been enforced since the
      nav refactor.** The table is repaired (screens→build, dashboard→home, market-health→market,
      backtests→build/backtests; `resultKey` stays `screens_bundle` so the historical series is
      continuous).
      With it measuring again, one route is **over**: `build/[id]` at **263.1 KB against 250 KB**,
      13.1 KB over. `build` itself is fine at 175.8 KB. The script's own advice is the fix — split
      with `next/dynamic` before raising the number — and that is this tree's work, not the TWT
      run's, so the gate is left honestly red rather than checked or re-budgeted.
  CHECK: cd decile-blueprint/apps/web && BASKFY_WEB_DIST_DIR=.next-gate node scripts/bundle-budget.mjs --check 2>&1 | tail -12
  EXPECT: within budget
  EVIDENCE: within budget — build/[id] **238.7 KB** against 250 KB, and `bundle-budget.mjs --check` exits 0 (12 Sep 2026). Was 263.1 KB. Fixed by code-splitting `DataTable` (@tanstack/react-table, 19.5 KB) and `PeekDrawer` out of `results-panel.tsx`, which is what docs/11's "after code-splitting the table" asks for and had never been done. `build` itself is 176.0 KB. Whole web suite 2978 passed; `pnpm run lint` 0 errors.
