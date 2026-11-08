# Gates: 7.2.2 Jargon on hover, reachable by keyboard

Scope: the header cell in `components/data/data-table.tsx` only.

Measured defect: the technical name is a native `title=` attribute, so keyboard and touch users
never see it. §1.3 says the human name is the header and the technical name lives behind it.

- [x] G1: The technical name is delivered by the repo's `Tooltip` primitive (or `TermHint`), not a
      bare `title=`.
  CHECK: cd decile-blueprint/apps/web && c=$(grep -c "title={headerTooltip}" src/components/data/data-table.tsx || true); echo "bare-title-attributes=$c"
  EXPECT: bare-title-attributes=0
  EVIDENCE: bare-title-attributes=0

- [x] G2: The trigger is focusable and reveals the technical name on keyboard focus.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/header-tooltip.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:05 | Duration  800ms (transform 46ms, setup 51ms, collect 114ms, tests 151ms, environment 267ms, prepare 40ms)

- [x] G3: Column sorting still works from the header, and the tooltip trigger does not swallow the
      sort activation.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/ 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:06 | Duration  1.12s (transform 188ms, setup 522ms, collect 691ms, tests 690ms, environment 2.26s, prepare 256ms)

- [x] G4: Every table that uses `DataTable` still renders — the header change is generic, so a
      regression here is repo-wide.
  CHECK: cd decile-blueprint/apps/web && pnpm run test 2>&1 | tail -8
  EXPECT: /Test Files +[0-9]+ passed \([0-9]+\)/
  EVIDENCE: Start at  01:16:21 | Duration  110.44s (transform 13.85s, setup 81.12s, collect 81.69s, tests 254.05s, environment 459.97s, prepare 42.65s)

- [x] G5: The §2.3 note about browser-side sorting not re-running the screen is still reachable.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/header-tooltip.test.tsx -t "G5" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:15:13 | Duration  2.43s (transform 84ms, setup 169ms, collect 310ms, tests 460ms, environment 1.06s, prepare 75ms)
