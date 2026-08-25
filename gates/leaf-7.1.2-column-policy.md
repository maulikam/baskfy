# Gates: 7.1.2 Column policy — no dead column, ever

Scope: `lib/screens/column-display.ts` + `src/lib/screens/__tests__/column-display.test.ts` ONLY.

Implements the interface pinned in PLAN.md ("Tree 7 interface contract"):
`visibleResultColumns(columns, rows?)` and `suppressedResultColumns(columns, rows)`.

Context: the brief's §1-defect-3 was a MARKETCAP column of em dashes. That specific column is
already dropped by the §2.2 diet, but the same fault is live on Price — `close_raw` is NULL for
all 271 seeded rows. Fix the class, not the instance.

- [x] G1: With rows supplied, a column whose value is null/undefined/"" in every row is dropped.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "empty" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:15:52 | Duration  2.87s (transform 235ms, setup 349ms, collect 690ms, tests 30ms, environment 1.28s, prepare 103ms)

- [x] G2: A column with even one non-empty value in any row is kept.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:15:56 | Duration  2.24s (transform 136ms, setup 186ms, collect 203ms, tests 233ms, environment 986ms, prepare 165ms)

- [x] G3: rank | symbol | name | sorting_factor are never dropped, even when empty.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "identity" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:15:59 | Duration  1.68s (transform 146ms, setup 166ms, collect 204ms, tests 3ms, environment 883ms, prepare 95ms)

- [x] G4: Called WITHOUT rows, behaviour is byte-identical to today's §2.2 diet, so existing
      callers and the existing tests are unaffected.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:01 | Duration  1.32s (transform 65ms, setup 82ms, collect 81ms, tests 153ms, environment 564ms, prepare 76ms)

- [x] G5: `suppressedResultColumns` returns exactly what was dropped for lack of data (and
      nothing dropped by the diet), so the UI can disclose it rather than hiding it silently.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "suppressed" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:03 | Duration  735ms (transform 59ms, setup 75ms, collect 76ms, tests 2ms, environment 331ms, prepare 48ms)

- [x] G6: `0`, `false` and `NaN` count as PRESENT data, not as empty — a screen of zero-return
      stocks must not lose its return column.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "zero" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:04 | Duration  831ms (transform 69ms, setup 104ms, collect 83ms, tests 2ms, environment 392ms, prepare 47ms)

- [x] G7: The §1.3 human label map is unchanged and still covers every key in the brief's table.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: Start at  22:16:06 | Duration  1.00s (transform 67ms, setup 92ms, collect 91ms, tests 162ms, environment 379ms, prepare 52ms)
