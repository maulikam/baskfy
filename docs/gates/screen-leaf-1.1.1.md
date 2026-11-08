# Gates: 1.1.1 One sticky header, scroll, FLIP, sort ⓘ

Scope: `data-table.tsx` — screen-safe header/scroll contract. Owns only this file + its test.

- [x] G1: `repeatHeaderEvery={0}` builds a display list with no repeat-header rows
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/data/__tests__/data-table-screen.test.tsx 2>&1 | tail -15
  EXPECT: passed
  EVIDENCE: Start at  22:17:54 | Duration  1.12s (transform 54ms, setup 106ms, collect 130ms, tests 177ms, environment 454ms, prepare 48ms)

- [x] G2: DataTable accepts optional `rowHeight` and `sortNote`
  CHECK: rg -n "rowHeight\?:|sortNote\?:" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/data/data-table.tsx
  EXPECT: sortNote
  EVIDENCE: 112:  rowHeight?: number; | 117:  sortNote?: string;

- [x] G3: sortNote renders as data-testid=sort-note (ⓘ), not a second header row
  CHECK: rg -n "sort-note" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/data/data-table.tsx
  EXPECT: sort-note
  EVIDENCE: 440:          data-testid="sort-note"

- [x] G4: client-side sort FLIP-animates rows, gated by motion-safe, duration ≤300ms
  CHECK: rg -n "motion-safe|duration-200|duration-150" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/data/data-table.tsx
  EXPECT: motion-safe
  EVIDENCE: 98: * `motion-safe:duration-200` must stay in lockstep with this number. | 190:        sortAnimating && "motion-safe:transition-transform motion-safe:duration-200",

- [x] G5: default ROW_HEIGHT.comfortable remains 34 for non-screen callers
  CHECK: rg -n "comfortable: 34" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/data/data-table.tsx
  EXPECT: comfortable: 34
  EVIDENCE: 85:export const ROW_HEIGHT: Record<Density, number> = { comfortable: 34, compact: 28 };
