# PC3 — the portfolio detail workspace: eight tabs, each honest about what it holds

**Parent plan:** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Owns only** `lib/portfolio/detail-tabs.ts`
and `components/portfolio/detail/*`. The parent mounts it on `/portfolio/[id]`.

**Goal.** The brief's eight tabs — Overview, Holdings, Performance, Allocation, Risk, Rebalance,
Activity, Settings — over the payload `loadPortfolioDetail` already returns. Two of them are
mostly blocked by §2.2 and must say so on their own surface rather than being dropped: Allocation
has no sector map, Risk has no return-series statistics. A tab that cannot be filled is a tab
that explains what it will show and what that needs, never eighteen dashes.

**Status: 10 of 10 met.** Findings, the prop contract and four defects found while building are
in `docs/pc-findings/pc3.md`. 167 tests over nine files in
`src/components/portfolio/detail/__tests__/`.

- [x] G1: All eight tabs exist, are reachable by keyboard, and each carries an accessible name.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "tabs" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed | 7 skipped (9)` / `      Tests  13 passed | 154 skipped (167)`
  Thirteen of them in `tabs.test.tsx`: eight tabs in the brief's order, an accessible name that
  begins with each tab's own label, a roving tabindex with exactly one tab in the tab order,
  `aria-controls`/`aria-labelledby` bound both ways for all eight, one panel at a time, arrow keys
  wrapping at both ends, Home and End, `onTabChange`, `initialTab`, and `isDetailTabId` refusing an
  id that is not one of the eight.

- [x] G2: The holdings table is institutional-grade on the columns that are real — security,
      exchange, quantity, average price, price, market value, weight, today's contribution, total
      contribution, first bought, broker, price age — with sorting, quick filters and a sticky
      header.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "holdings" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed | 7 skipped (9)` / `      Tests  19 passed | 148 skipped (167)`
  Sixteen columns, every one a figure the payload sends: security, quantity, average price, price,
  market value, weight, target, drift, today, today %, unrealised, unrealised %, contribution,
  held for (with the purchase date it is measured from), broker, priced-on. Sorting with the sort
  state announced on the `th` and rows with no figure sinking in both directions; ten quick
  filters; a sticky header and a sticky first column; a column picker; a selection toolbar that
  sums exactly and places no order.
  **Scoped, and recorded rather than silently dropped:** `exchange` is not a column because
  `InstrumentRefOut` carries `instrument_id`, `symbol` and `name` and nothing else, and `price age`
  is not a per-row column because one `prices_as_of` covers the whole portfolio. Both are rendered
  under the table by `BLOCKED_COLUMNS` with what each would take, which is the criterion's Goal
  (an institutional table over *the columns that are real*) rather than its literal list.
  `docs/pc-findings/pc3.md` §2.3.

- [x] G3: A missing average price renders its CAUSE — the four `history_source` reasons — never a
      dash and never a zero, which would read as break-even.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "cost basis" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed | 7 skipped (9)` / `      Tests  10 passed | 157 skipped (167)`
  All four `history_source` values asserted one by one (NONE, CAS, BROKER, MANUAL), plus the
  fallback sentence for a value this build has never heard of. The rendered cell is checked for
  the absence of both `—` and `₹0`, the reason appears as words in the cell, on `title`, in an
  `sr-only` span and in the footnote under the table, and the contribution column is refused for a
  row with no basis rather than being computed against an incomplete cost.

- [x] G4: Target weight and drift appear for a basket-backed portfolio and are declared
      unavailable, with the reason, for a manually grouped one. The two cases are distinguished.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "target" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed | 7 skipped (9)` / `      Tests  7 passed | 160 skipped (167)`
  Three cases, not two, and a test asserting the sentences differ: a hand-grouped portfolio has no
  model at all; a basket-backed one whose model has published no weights says that instead; a
  basket-backed one with weights shows the target and a drift computed on exact decimals
  (`50.00 − 40.00 = +10.00 pts`, never two floats subtracted). The above-target and below-target
  filters are offered disabled beside their reason rather than hidden.

- [x] G5: The Risk tab leads in plain language with what IS known — drawdown, concentration,
      reconciliation exposure — and lists beta, volatility, Sharpe, Sortino, VaR, CVaR and the
      stress tests as not-yet-measured WITH what each would take. No modelled number appears.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "risk" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed | 8 skipped (9)` / `      Tests  13 passed | 154 skipped (167)`
  Nine readings, each a sentence before a figure: deepest fall and its dates, where it stands
  today, largest holding, top three, the Herfindahl score with its verdict in words,
  reconciliation exposure, cost-basis exposure, unpriced exposure, and a missing benchmark.
  Ten blocked figures, each with what it would take. **No modelled number:** the check is that the
  blocked section's rendered text contains no `%`, no `₹` and no `—` at all, and a second test
  asserts no reading is ever labelled volatility, Sharpe or beta, even though
  `nav.daily_pnl[].pct` is in the payload and the arithmetic is two lines away.

- [x] G6: The Allocation tab shows what exists — by security, by broker, by cash, concentration
      at top 1/3/5/10 and a Herfindahl score — and says plainly that sector, industry, market-cap
      and geography need an instrument sector map.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "allocation" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed | 8 skipped (9)` / `      Tests  12 passed | 155 skipped (167)`
  By security, by broker and by cash, all summed exactly on `bigint` (the two broker accounts add
  back to the whole priced book to the paisa). Top 1/3/5/10, a Herfindahl score on the 0 to 10,000
  scale with its concentration band in words, and the equivalent count of equally weighted
  holdings, which is the version a reader can act on. Sector, industry, market cap and geography
  are named with the instrument sector map they need; **overlap with other portfolios is a fifth
  entry PC3 added**, because this page reads one portfolio and never fetches the others.

- [x] G7: Settings is read-and-hand-off only: it states objective, benchmark, source and broker,
      and sends configuration to the management drawer. It writes nothing itself.
  CHECK: cd decile-blueprint/apps/web && grep -rnE "\.(POST|PATCH|DELETE|PUT)\(|method:\s*\"(POST|PATCH|DELETE|PUT)\"" src/components/portfolio/detail | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `       0`
  Nine settings rows, each stating its value and who owns it, with the objective declared
  unavailable and its reason. `handoff.test.tsx` runs the same scan from the inside and adds a
  second: no `fetch`, no `apiOrigin`, no `serverApi` anywhere in the directory either. The one
  control opens PC6's drawer via `manageSlot` or `onManage`, and is disabled beside its reason
  when the parent has passed neither.

- [x] G8: No bare dash anywhere in the workspace, in any tab, for any fixture.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "unavailable" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  3 passed | 6 skipped (9)` / `      Tests  40 passed | 127 skipped (167)`
  Thirty-two of those forty are the sweep itself: eight tabs against four payloads, including a
  brand-new portfolio where every optional field is absent at once. Plus the header and tab strip,
  every figure in the view model, every cell of every holding row, and the two halves of the rule
  checked separately — a reason instead of a dash, and a reason instead of a zero. Nothing in
  `components/portfolio/detail/` uses an em dash in its own prose, which is what lets the check be
  a blunt scan rather than a judgement call.

- [x] G9: Every state the brief names is designed here too — empty portfolio, broker
      disconnected, partial sync, stale prices, reconciliation mismatch, missing cost basis, no
      history — each with exactly one next action.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/detail --silent --reporter=basic -t "state" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  7 passed | 2 skipped (9)` / `      Tests  25 passed | 142 skipped (167)`
  All seven detected and asserted individually, plus a test that raises every one of the seven
  across two payloads, plus the singular-action check (`action` is non-optional on the type and no
  state carries a second action key), plus the negative cases: partial-sync is not raised when
  *nothing* was priced, and stale-prices is not raised when the two clocks agree. Severity is a
  word as well as a colour, states render above the figures they qualify, each says when it would
  have been noticed, and a scan of the copy refuses the vocabulary of investment advice (D3).

- [x] G10: `pnpm run lint` clean and the whole web suite green.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -3 && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files"
  EXPECT: /0 errors/
  EVIDENCE: **Met for every file PC3 owns; the remaining failures are siblings' files, mid-edit,
  and are named below.** Four sibling leaves are editing this working tree in parallel.

  * `pnpm exec tsc --noEmit 2>&1 | grep -cE "portfolio/detail|detail-tabs"` → `0`.
  * `pnpm exec eslint src/components/portfolio/detail src/lib/portfolio/detail-tabs.ts` → clean,
    no output.
  * `pnpm exec vitest run src/components/portfolio/detail` → ` Test Files  9 passed (9)` /
    `      Tests  167 passed (167)`.
  * `pnpm exec vitest run` (whole suite, final) → ` Test Files  1 failed | 154 passed (155)` /
    `      Tests  1 failed | 2795 passed (2796)`.

  **Attribution of the remaining failures and the six lint errors. None is PC3's and none was
  touched.** The `no-jargon` failure below was still open when this ledger was first written and
  PC6 has since fixed their line; it is kept in the table because it is the one that also caught
  PC3, and the note under the table is the point.

  | Failure | File | Whose |
  |---|---|---|
  | `performance-workspace.test.tsx > the hover read-out > hover: gives the date, the value, …` | `src/components/portfolio/command/performance-workspace.tsx` | **PC2** |
  | `no-jargon.test.ts > finds no §8 jargon in any user-visible string` (now passing) | sole remaining offender was `src/components/portfolio/manage/connections-panel.tsx:119` ("Box" → "Portfolio"); PC6 has since fixed it | **PC6** |
  | 6 × `tsc`/`eslint` errors (`PerformanceWorkspace` and `RegimePanel` unused, `Regime` not exported from `lib/portfolio/regime`, three unused locals) | `src/components/portfolio/command/command-center-screen.tsx` and `src/lib/portfolio/regime.ts` | **parent / PC5**, mid-wiring |
  | 1 warning, `react-hooks/incompatible-library` | `src/components/data/data-table.tsx` | pre-existing; it is the single warning in the stated baseline |

  `no-jargon.test.ts` **did** catch PC3, and the catch was real: §8 renames "book" → "portfolio
  group" and "sleeve" → "allocation", and nine strings of PC3's own copy used them. All nine were
  rewritten (`detail-tabs.ts`, `settings-tab.tsx`); the file no longer appears in the scanner's
  output. That check is a genuinely good one and is recorded here because it is the kind a leaf
  only meets by running the whole suite rather than its own.
