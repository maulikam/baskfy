# Gates: 7.1.1 The cells tell the truth

Scope: `components/screens/cell-encodings.tsx` + `components/screens/result-columns.tsx` and their
tests. Covers the score bar's scale and colour, the bumpiness dots, the rank badge, and the Price
cell's honesty.

- [x] G1: A score above 3 no longer paints the same width as 3. Two different scores above 3
      produce two different widths.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "proportional" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:28 | Duration  1.05s (transform 67ms, setup 112ms, collect 54ms, tests 25ms, environment 555ms, prepare 130ms)

- [x] G2: The bar's scale is derived from the rows on screen, not a magic constant. The largest
      score paints full width; half of it paints about half.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:30 | Duration  721ms (transform 34ms, setup 68ms, collect 42ms, tests 83ms, environment 307ms, prepare 40ms)

- [x] G3: Zero, negative and non-finite scores render without a negative width and without
      throwing.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "negative" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:31 | Duration  631ms (transform 32ms, setup 57ms, collect 39ms, tests 19ms, environment 295ms, prepare 42ms)

- [x] G4: The bar fill uses the brand/accent fill token rather than `bg-foreground/70`, and
      `text-brand` appears nowhere (a repo test fails the build on it).
  CHECK: cd decile-blueprint/apps/web && rg -c "bg-foreground/70" src/components/screens/cell-encodings.tsx; npx vitest run src/lib/__tests__/no-brand-as-text.test.ts src/lib/__tests__/contrast.test.ts 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: Start at  22:15:49 | Duration  990ms (transform 47ms, setup 221ms, collect 48ms, tests 29ms, environment 983ms, prepare 103ms)

- [x] G5: The bumpiness dots' ceiling is justified against the real spread of `vol_12m` instead of
      a magic 0.8, and the least volatile row does not show the same dot count as the median.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "bumpiness" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:32 | Duration  692ms (transform 37ms, setup 62ms, collect 45ms, tests 1ms, environment 332ms, prepare 36ms)

- [x] G6: Ranks 1–3 expose their number as text to assistive tech; rank > 3 stays a plain
      right-aligned figure; the dots keep a non-visual equivalent.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "rank" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:33 | Duration  766ms (transform 35ms, setup 60ms, collect 44ms, tests 65ms, environment 356ms, prepare 39ms)

- [x] G7: The Price cell never shows an adjusted close under a label promising the exchange print
      without saying so, and `/build/exmpl0000001` no longer renders a column of 271 em dashes.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-columns.test.tsx 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:34 | Duration  636ms (transform 37ms, setup 58ms, collect 36ms, tests 2ms, environment 336ms, prepare 36ms)

- [ ] G8: Numbers stay `tabular-nums` and every encoding still works in dark mode via semantic
      tokens only.
  EVIDENCE: pending

- [x] G9: The Price/dead-column decision is recorded in docs/DECISIONS-MERGE.md, tagged
      UNREVIEWED, naming the reversal.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && rg -c "close_raw" docs/DECISIONS-MERGE.md
  EXPECT: /[1-9]/
  EVIDENCE: 10
