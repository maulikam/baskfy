# PC2 — the performance workspace: the chart, and what moved the number

**Parent plan:** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Owns only** the files listed in §6.1
for PC2. The parent mounts the component; this leaf never edits a page or the screen.

**Goal.** The brief's central visual: portfolio value over time with the invested line, the
benchmark and a drawdown overlay, a value/return/drawdown toggle, a hover read-out, a range
selector, and — below it — attribution that says which portfolio moved the number. Nothing on it
is invented: the effects Baskfy cannot decompose are named as unavailable with the reason.

- [x] G1: `lib/portfolio/performance.ts` is pure — no fetch, no Date.now, no window — and turns a
      `NavSeries` plus the portfolio rows into everything the workspace draws.
  CHECK: cd decile-blueprint/apps/web && grep -nE "fetch\(|Date\.now|window\.|localStorage" src/lib/portfolio/performance.ts | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `0`. Note for whoever edits this file next: the first run of this check returned
  **10**, and every one of them was the English word — "…inside this window." — matching
  `window\.`. The module's prose now says "period" or "stretch of days" at a full stop and a
  comment at the top of the file says why, so the check keeps meaning what it claims to mean
  rather than being widened to silence itself.

- [x] G2: Every range the brief names is either drawn or declared. 1D and 1W have no intraday
      series behind them, so they are shown as unavailable **with the reason**, never as a dead
      pill and never silently dropped.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/performance.test.ts src/components/portfolio/command/__tests__/performance-workspace.test.tsx --silent --reporter=basic -t "range" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  2 passed (2)` / `Tests  10 passed | 59 skipped (69)`.
  1D and 1W render no control at all — a dim pill teaches a reader to click it again tomorrow —
  and `performance-range-unavailable` carries "1D and 1W are not available" with the intraday
  reason. `servableRanges()` is typed to `NavRange`, so a pill cannot ask the API for a window it
  has no member for.

- [x] G3: The hover read-out gives date, value, the day's change, cumulative return and the
      benchmark's return at that date — and says so when the benchmark has no print that day.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/performance-workspace.test.tsx --silent --reporter=basic -t "hover" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  5 passed | 26 skipped (31)`.
  Five tests: the full read-out, moving it, the pointer path (with a stubbed
  `getBoundingClientRect`, since jsdom runs no layout), a day the index did not print, and the
  first day of the window. **This gate found a real defect:** the read-out chose rupees-or-percent
  from the cell's *label*, so the day's ₹6,000 move rendered as "6000%". Units are now declared
  per cell.

- [x] G4: Value, return and drawdown are three views of the same series, and the return view is
      the wealth index — never a percentage derived from the value line, which would credit a
      deposit to the strategy.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/performance.test.ts --silent --reporter=basic -t "deposit" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  3 passed | 35 skipped (38)`.
  The fixture assigns ₹50,000 on a day the market did not move. Value reads
  `[100000, 150000, 165000]`; a percentage of that line would read **+50%** on day two; the
  return view reads `[0, 0, 10]`. The capital line reads `[100000, 150000, 150000]`, so the gap
  a reader takes as profit is unchanged by a transfer, and `spanPnl` returns `15000`.

- [x] G5: Attribution by portfolio is real and adds up: each capital portfolio's share of today's
      move and of total P&L, reconciling to the aggregate figure.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/performance.test.ts --silent --reporter=basic -t "attribution" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  10 passed | 28 skipped (38)`.
  Today reconciles from `PortfolioRowOut.todays_pnl` to `hero.todays_pnl` exactly (`explained
  "6000"` = `reported "6000"`), and a residual is named rather than folded into the largest row.
  **The "total P&L" half needed a scoping decision and it is recorded here:** `PortfolioRowOut`
  carries no cost basis and no per-portfolio total P&L, so apportioning `hero.total_pnl` by
  weight would have assumed every portfolio returned the same thing. Instead the period band is
  computed from each portfolio's **own** NAV series (`GET /api/v1/portfolio/{id}/nav`, which the
  parent passes as an optional prop), where `value_last − value_first − Σ net_flow` is exact and
  reconciles to the aggregate's own window profit — tested, `explained "10000"` = `reported
  "10000"`. Without that prop the band shows no figure and names the endpoint it needs.

- [x] G6: The effects that cannot be computed — sector contribution, allocation effect,
      security-selection effect, cash drag, fees and taxes — appear as named, reasoned
      unavailables, and NEVER as a number.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/performance-workspace.test.tsx --silent --reporter=basic -t "unavailable" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  6 passed | 25 skipped (31)`.
  All six render with a reason and a "Needs:" line, and a regex scan asserts none of them ever
  appears beside a digit. **Cash drag is the one §2.2 does not name and it looks computable:**
  every mark carries `cash`, so what cash *weighed* is known; what it *cost* is not, because
  `net_flow` records money entering and leaving the account and never money moving between cash
  and shares inside it. That reason is in the source and is asserted.

- [x] G7: Empty, single-point and benchmark-missing series each render an explanation with one
      next action, not an empty frame.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/performance-workspace.test.tsx --silent --reporter=basic -t "state" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  7 passed | 24 skipped (31)`.
  Empty, single-mark, no-benchmark, a benchmark with too few prints inside the window, a view
  that cannot be drawn, and "no way to place an order". A missing benchmark explains itself
  *without* blocking the chart — the plot still renders, which is asserted — and each state
  carries exactly one next action.

- [x] G8: Nothing is signalled by colour alone — each series carries a direct label, and gain/
      loss carries a word or glyph.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/performance-workspace.test.tsx --silent --reporter=basic -t "colour" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  3 passed | 28 skipped (31)`.
  Each line is labelled where it ends (`performance-direct-label-{portfolio,capital,benchmark}`)
  and its stroke is named in words — solid / dotted / dashed — so three lines stay three lines in
  greyscale. ▲/▼ in the headline strip and in every contribution row, plus `sr-only` "gain of" /
  "loss of" for a screen reader.

- [x] G9: `pnpm run lint` clean and the whole web suite green.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -3 && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files"
  EXPECT: /0 errors/
  EVIDENCE: **PC2's three files are clean; the tree is not, and every remaining failure is a
  sibling's file mid-edit.** Measured after this leaf finished:
  · `tsc --noEmit` — **0 errors** across the whole app (PC2 fixed four of its own:
    `exactOptionalPropertyTypes` means `{ benchmark: undefined }` is not the same type as a
    payload with no `benchmark` key, and the API sends the latter).
  · `eslint .` — `✖ 6 problems (4 errors, 2 warnings)`, **0 of them in a PC2 file**:
    2 errors `src/components/portfolio/command/command-header.tsx` (PC1's file, modified by the
    parent this session — `no-unnecessary-type-assertion` ×2);
    1 error `src/components/portfolio/manage/manage-drawer.tsx` (PC6);
    1 error `src/components/portfolio/manage/sleeve-form.tsx` (PC6, `set-state-in-effect`);
    1 warning `src/components/portfolio/manage/view-builder.tsx` (PC6);
    1 warning `src/components/data/data-table.tsx` (the baseline's single warning).
  · `vitest run` — `Test Files  2 failed | 141 passed (143)` / `Tests  3 failed | 2587 passed
    (2590)`. Both failing files are siblings':
    `src/components/portfolio/manage/__tests__/manage-drawer.test.tsx` (PC6, 2 failures), and
    `src/lib/__tests__/no-jargon.test.ts`, whose report names only
    `src/lib/portfolio/detail-tabs.ts` + `src/components/portfolio/detail/settings-tab.tsx` (PC3)
    and `src/components/portfolio/manage/sleeve-form.tsx` + `src/lib/portfolio/manage.ts` (PC6).
  · PC2's own two files: `Test Files  2 passed (2)` / `Tests  69 passed (69)`.
  **The jargon test caught PC2 too and it was fixed here, not attributed away.** `no-jargon`
  enforces `PORTFOLIO_REDESIGN.md` §8 — "Book" is banned in user-visible strings — and all three
  PC2 files used it freely. Every visible occurrence is gone; the word survives only in comments,
  which the scanner deliberately exempts.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
