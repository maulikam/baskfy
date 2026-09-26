# Gates: OV12 — the candidates table sorts by any column (26 Sep 2026)

Maulik: "Name On Strategies Price Results Filing Laya Screens — I want to have the table which
can be sorted in any of the ways I select on the UI." Scope: client-side sort on `/build/overlap`'s
candidates table, every one of the eight headers a toggle, the server's order (count, actionable,
symbol) kept as the starting order. Not in scope: a server-side `sort=` parameter, persisting the
choice.

- [x] G1: `sortCandidates(rows, key, direction)` in `lib/overlap/candidates.ts` is pure, covers all eight keys, keeps nulls last in both directions, and is unit-tested
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/overlap 2>&1 | grep -E "Tests|passed|failed" | tail -2
  EXPECT: /passed/
  EVIDENCE: lib/overlap: Tests 33 passed (33) (22 existing + 11 sort-candidates)

- [x] G2: Every header is a button with `aria-sort`; the first click uses the column's natural direction (text ascends, figures descend), the second flips it; the row order in the DOM follows
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "aria-sort" src/components/overlap/candidates-table.tsx && pnpm exec vitest run src/components/overlap -t "sort" 2>&1 | grep -E "^\s+Tests" | tail -1
  EXPECT: /Tests\s+[3-9] passed/
  EVIDENCE: aria-sort occurrences in candidates-table.tsx: 1; vitest -t sort: Tests 4 passed | 27 skipped (31)

- [x] G3: tsc and eslint clean for the web app
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit && pnpm exec eslint src/lib/overlap src/components/overlap && echo clean
  EXPECT: /^clean$/
  EVIDENCE: tsc --noEmit exit 0; eslint src/lib/overlap src/components/overlap: 0 problems (the one useMemo warning fixed with a module-level NO_ROWS constant)

- [x] G4: The intro copy says the order is the server's until a column is chosen; DECISIONS-MERGE entry (⚠ UNREVIEWED) with the per-column meaning of "ascending"
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "sorts by any column" docs/DECISIONS-MERGE.md
  EXPECT: /[1-9]/
  EVIDENCE: intro copy: 'until you pick a column: every header sorts, and a second click turns it round'; DECISIONS-MERGE 'the candidates table sorts by any column' (UNREVIEWED)

- [ ] G5: One commit `OV12: green — …`, tree clean apart from holdings-status/
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git log -1 --format=%s | cut -c1-12 && git status --porcelain | grep -v "^?? holdings-status/" | wc -l
  EXPECT: /OV12: green/
  EVIDENCE: pending

- [ ] G6: Deployed with ship.sh from a detached worktree outside the 18:40–21:15 nightly window; the page's headers are buttons on staging
  EVIDENCE: pending
