# Gates: 7.1.2 Column policy — no dead column, ever

Scope: `lib/screens/column-display.ts` + `src/lib/screens/__tests__/column-display.test.ts` ONLY.

Implements the interface pinned in PLAN.md ("Tree 7 interface contract"):
`visibleResultColumns(columns, rows?)` and `suppressedResultColumns(columns, rows)`.

Context: the brief's §1-defect-3 was a MARKETCAP column of em dashes. That specific column is
already dropped by the §2.2 diet, but the same fault is live on Price — `close_raw` is NULL for
all 271 seeded rows. Fix the class, not the instance.

- [x] G1: With rows supplied, a column whose value is null/undefined/"" in every row is dropped.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "empty" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:51 | Duration  1.79s (transform 96ms, setup 346ms, collect 116ms, tests 3ms, environment 908ms, prepare 156ms)

- [x] G2: A column with even one non-empty value in any row is kept.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:48 | Duration  701ms (transform 44ms, setup 51ms, collect 55ms, tests 118ms, environment 259ms, prepare 42ms)

- [x] G3: rank | symbol | name | sorting_factor are never dropped, even when empty.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "identity" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:54 | Duration  1.97s (transform 89ms, setup 161ms, collect 205ms, tests 2ms, environment 1.09s, prepare 186ms)

- [x] G4: Called WITHOUT rows, behaviour is byte-identical to today's §2.2 diet, so existing
      callers and the existing tests are unaffected.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:50 | Duration  728ms (transform 46ms, setup 51ms, collect 56ms, tests 114ms, environment 264ms, prepare 43ms)

- [x] G5: `suppressedResultColumns` returns exactly what was dropped for lack of data (and
      nothing dropped by the diet), so the UI can disclose it rather than hiding it silently.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "suppressed" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:57 | Duration  1.73s (transform 96ms, setup 192ms, collect 150ms, tests 4ms, environment 958ms, prepare 81ms)

- [x] G6: `0`, `false` and `NaN` count as PRESENT data, not as empty — a screen of zero-return
      stocks must not lose its return column.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts -t "zero" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:59 | Duration  2.07s (transform 77ms, setup 161ms, collect 152ms, tests 2ms, environment 1.32s, prepare 244ms)

- [x] G7: The §1.3 human label map is unchanged and still covers every key in the brief's table.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/column-display.test.ts 2>&1 | tail -6
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:53 | Duration  696ms (transform 43ms, setup 51ms, collect 54ms, tests 116ms, environment 268ms, prepare 37ms)
