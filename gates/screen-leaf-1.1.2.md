# Gates: 1.1.2 Dead columns, disclaimer once, empty undo

Scope: `results-panel.tsx` — wire diet/dead-column policy, disclose suppressions, empty undo.

- [x] G1: buildColumns is called with rows so dead MARKETCAP/Price columns drop
  CHECK: rg -n "buildColumns" -A 8 /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/results-panel.tsx
  EXPECT: rows
  EVIDENCE: 164-    [result?.columns, result?.sorting_factor.label, columnMeta, sortingFactorUnit, rows], | 165-  );

- [x] G2: suppressed columns are disclosed (data-testid=suppressed-columns)
  CHECK: rg -n "suppressed-columns|suppressedResultColumns" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/results-panel.tsx
  EXPECT: suppressed
  EVIDENCE: 168:    () => (result ? suppressedResultColumns(result.columns, rows) : []), | 217:          data-testid="suppressed-columns"

- [x] G3: empty state offers undo-last-filter (testid) in addition to reset
  CHECK: rg -n "undo-last-filter|onLoosenFilters" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/results-panel.tsx
  EXPECT: undo-last-filter
  EVIDENCE: 231:                ? { label: "Reset filters to defaults", onClick: onLoosenFilters } | 239:            data-testid="undo-last-filter"

- [x] G4: screen table passes rowHeight={52} and sortNote (paragraph under table removed or hidden)
  CHECK: rg -n "rowHeight|sortNote" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/results-panel.tsx
  EXPECT: rowHeight
  EVIDENCE: 123:      rowHeight={density === "compact" ? 36 : 52} | 124:      sortNote={SORT_NOTE}

- [x] G5: results-panel tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/screens/__tests__/results-panel.test.tsx 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: Start at  22:27:47 | Duration  2.39s (transform 214ms, setup 137ms, collect 531ms, tests 529ms, environment 672ms, prepare 159ms)

- [x] G6: screens components still do not import Disclaimer
  CHECK: rg -n "import \{ Disclaimer \}" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE
