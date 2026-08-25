# Gates: 7.2.1 Chip labels are human at every moment

Scope: new `lib/screens/universe-label.ts` + the label call sites in `filter-chip-bar.tsx`.

Measured defect: the chip shows the raw slug `nifty-total-market` until `/meta/universes`
resolves, then the API's `NIFTY TOTAL MARKET`. §1.1 asks for `NIFTY Total Market`.

- [x] G1: A raw slug never reaches the chip. Before the universe list loads, the label is either a
      slug-derived human string or a skeleton — never `nifty-total-market`.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/universe-label.test.ts -t "slug" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:08 | Duration  1.72s (transform 180ms, setup 105ms, collect 409ms, tests 87ms, environment 532ms, prepare 127ms)

- [x] G2: `NIFTY TOTAL MARKET` renders as `NIFTY Total Market`; `NIFTY 50` stays `NIFTY 50`;
      `All NSE Listed Stocks` is left alone. All 14 real universe names are covered.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/universe-label.test.ts 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:23:02 | Duration  1.79s (transform 156ms, setup 117ms, collect 388ms, tests 338ms, environment 615ms, prepare 48ms)

- [ ] G3: The chip's accessible name equals its visible label.
  EVIDENCE: pending

- [x] G4: The sort-by chip still shows the human factor name (`Consistency score`, not
      `AVERAGE SHARPE RETURN 12 6 3 1 MONTHS`).
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: Start at  22:16:12 | Duration  1.59s (transform 72ms, setup 76ms, collect 89ms, tests 146ms, environment 510ms, prepare 76ms)
