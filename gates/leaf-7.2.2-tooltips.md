# Gates: 7.2.2 Jargon on hover, reachable by keyboard

Scope: the header cell in `components/data/data-table.tsx` only.

Measured defect: the technical name is a native `title=` attribute, so keyboard and touch users
never see it. §1.3 says the human name is the header and the technical name lives behind it.

- [x] G1: The technical name is delivered by the repo's `Tooltip` primitive (or `TermHint`), not a
      bare `title=`.
  CHECK: cd decile-blueprint/apps/web && rg -n "title=\{headerTooltip\}" src/components/data/data-table.tsx | wc -l
  EXPECT: 0
  EVIDENCE: 0

- [x] G2: The trigger is focusable and reveals the technical name on keyboard focus.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/header-tooltip.test.tsx 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:35 | Duration  870ms (transform 49ms, setup 61ms, collect 145ms, tests 172ms, environment 285ms, prepare 40ms)

- [x] G3: Column sorting still works from the header, and the tooltip trigger does not swallow the
      sort activation.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/ 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:14 | Duration  2.21s (transform 188ms, setup 451ms, collect 522ms, tests 408ms, environment 3.40s, prepare 264ms)

- [x] G4: Every table that uses `DataTable` still renders — the header change is generic, so a
      regression here is repo-wide.
  CHECK: cd decile-blueprint/apps/web && pnpm run test 2>&1 | tail -8
  EXPECT: /Test Files .* passed/
  EVIDENCE: Duration  21.26s (transform 3.45s, setup 23.73s, collect 14.16s, tests 23.26s, environment 101.64s, prepare 8.15s) | [ELIFECYCLE] Test failed. See above for more details.

- [ ] G5: The §2.3 note about browser-side sorting not re-running the screen is still reachable.
  EVIDENCE: pending
