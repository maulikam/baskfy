# Gates: 1.1 Defects & table chrome (integration)

Scope: children 1.1.1 + 1.1.2 merged — one sticky header, dead columns gone, disclaimer once

- [x] N1: child leaves fully checked
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/screen-leaf-1.1.1.md gates/screen-leaf-1.1.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/screen-leaf-1.1.2.md: 6 gates | ALL MET (11 met)

- [x] N2: results panel uses the new DataTable props from the contract
  CHECK: rg -n "sortNote|rowHeight|repeatHeaderEvery" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/results-panel.tsx
  EXPECT: sortNote
  EVIDENCE: 123:      rowHeight={rowHeightFor(density)} | 124:      sortNote={SORT_NOTE}

- [x] N3: targeted tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/data/__tests__/data-table-screen.test.tsx src/components/screens/__tests__/results-panel.test.tsx 2>&1 | tail -15
  EXPECT: passed
  EVIDENCE: Start at  22:18:01 | Duration  1.02s (transform 60ms, setup 87ms, collect 140ms, tests 203ms, environment 335ms, prepare 46ms)
