# Gates: 7.1.1 The cells tell the truth

Scope: `components/screens/cell-encodings.tsx` + `components/screens/result-columns.tsx` and their
tests. Covers the score bar's scale and colour, the bumpiness dots, the rank badge, and the Price
cell's honesty.

- [x] G1: A score above 3 no longer paints the same width as 3. Two different scores above 3
      produce two different widths.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "proportional" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:39 | Duration  1.83s (transform 59ms, setup 200ms, collect 89ms, tests 51ms, environment 1.03s, prepare 70ms)

- [x] G2: The bar's scale is derived from the rows on screen, not a magic constant. The largest
      score paints full width; half of it paints about half.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:40 | Duration  638ms (transform 32ms, setup 51ms, collect 39ms, tests 83ms, environment 274ms, prepare 45ms)

- [x] G3: Zero, negative and non-finite scores render without a negative width and without
      throwing.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "negative" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:42 | Duration  1.95s (transform 73ms, setup 182ms, collect 97ms, tests 135ms, environment 1.11s, prepare 89ms)

- [x] G4: The bar fill uses the brand/accent fill token rather than `bg-foreground/70`, and
      `text-brand` appears nowhere (a repo test fails the build on it).
  CHECK: cd decile-blueprint/apps/web && test "$(grep -c 'bg-foreground/70' src/components/screens/cell-encodings.tsx)" = 0 && npx vitest run src/lib/__tests__/no-brand-as-text.test.ts src/lib/__tests__/contrast.test.ts 2>&1 | tail -6
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:42 | Duration  692ms (transform 33ms, setup 131ms, collect 34ms, tests 99ms, environment 597ms, prepare 71ms)

- [x] G5: The bumpiness dots' ceiling is justified against the real spread of `vol_12m` instead of
      a magic 0.8, and the least volatile row does not show the same dot count as the median.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "bumpiness" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:45 | Duration  2.02s (transform 70ms, setup 229ms, collect 153ms, tests 2ms, environment 1.12s, prepare 251ms)

- [x] G6: Ranks 1–3 expose their number as text to assistive tech; rank > 3 stays a plain
      right-aligned figure; the dots keep a non-visual equivalent.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "rank" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:48 | Duration  1.99s (transform 66ms, setup 247ms, collect 109ms, tests 220ms, environment 957ms, prepare 86ms)

- [x] G7: The Price cell never shows an adjusted close under a label promising the exchange print
      without saying so, and `/build/exmpl0000001` no longer renders a column of 271 em dashes.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-columns.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:45 | Duration  576ms (transform 36ms, setup 50ms, collect 36ms, tests 2ms, environment 256ms, prepare 33ms)

- [x] G8: Numbers stay `tabular-nums` and every encoding still works in dark mode via semantic
      tokens only.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/cell-encodings.test.tsx -t "G8" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:46 | Duration  591ms (transform 31ms, setup 50ms, collect 37ms, tests 22ms, environment 255ms, prepare 48ms)

- [x] G9: The Price/dead-column decision is recorded in docs/DECISIONS-MERGE.md, tagged
      UNREVIEWED, naming the reversal.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "close_raw" docs/DECISIONS-MERGE.md
  EXPECT: /[1-9]/
  EVIDENCE: 15
