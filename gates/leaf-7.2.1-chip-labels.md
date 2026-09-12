# Gates: 7.2.1 Chip labels are human at every moment

Scope: new `lib/screens/universe-label.ts` + the label call sites in `filter-chip-bar.tsx`.

Measured defect: the chip shows the raw slug `nifty-total-market` until `/meta/universes`
resolves, then the API's `NIFTY TOTAL MARKET`. §1.1 asks for `NIFTY Total Market`.

- [x] G1: A raw slug never reaches the chip. Before the universe list loads, the label is either a
      slug-derived human string or a skeleton — never `nifty-total-market`.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/universe-label.test.ts -t "slug" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:09 | Duration  2.80s (transform 232ms, setup 212ms, collect 696ms, tests 329ms, environment 1.14s, prepare 62ms)

- [x] G2: `NIFTY TOTAL MARKET` renders as `NIFTY Total Market`; `NIFTY 50` stays `NIFTY 50`;
      `All NSE Listed Stocks` is left alone. All 14 real universe names are covered.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/universe-label.test.ts 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:01 | Duration  984ms (transform 95ms, setup 50ms, collect 205ms, tests 232ms, environment 280ms, prepare 42ms)

- [x] G3: The chip's accessible name equals its visible label.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/filter-chip-bar.test.tsx -t "G3" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:12 | Duration  2.73s (transform 241ms, setup 341ms, collect 663ms, tests 178ms, environment 1.10s, prepare 85ms)

- [x] G4: The sort-by chip still shows the human factor name (`Consistency score`, not
      `AVERAGE SHARPE RETURN 12 6 3 1 MONTHS`).
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -6
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:04 | Duration  679ms (transform 42ms, setup 50ms, collect 55ms, tests 120ms, environment 259ms, prepare 37ms)
