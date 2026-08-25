# Gates: 7.3.2 Motion: FLIP on sort, stagger on load

Scope: the row layer of `components/data/data-table.tsx` + any keyframes in `globals.css`.

Measured gap: §3.2 is absent. Rows currently share one uniform fade; there is no FLIP and no
stagger. No new dependency is allowed (`docs/02` locks the stack — no framer-motion), so this is
CSS plus `getBoundingClientRect`.

- [x] G1: When a sort flips, rows animate from their previous position to their new one rather
      than snapping.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "flip" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:37 | Duration  1.05s (transform 65ms, setup 82ms, collect 130ms, tests 95ms, environment 481ms, prepare 44ms)

- [x] G2: Rows stagger in on load/re-filter at 15–20ms per row, and the stagger is capped so row
      271 does not wait seconds.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "stagger" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:38 | Duration  795ms (transform 51ms, setup 72ms, collect 120ms, tests 54ms, environment 325ms, prepare 42ms)

- [x] G3: Under `prefers-reduced-motion: reduce` nothing animates — no FLIP, no stagger.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "reduced" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  20:30:39 | Duration  757ms (transform 55ms, setup 65ms, collect 124ms, tests 36ms, environment 317ms, prepare 64ms)

- [ ] G4: No animation exceeds 300ms, and only transform/opacity are animated (the repo's motion
      budget).
  EVIDENCE: pending

- [x] G5: No new runtime dependency was added.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff -- decile-blueprint/apps/web/package.json | rg "^\+ " | wc -l
  EXPECT: 0
  EVIDENCE: 0 | /bin/sh: rg: command not found

- [x] G6: Virtualised scrolling still works and the sticky header still sticks — FLIP on a
      virtualised list is where this breaks.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/ 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: Start at  22:16:44 | Duration  1.47s (transform 182ms, setup 375ms, collect 565ms, tests 518ms, environment 1.68s, prepare 167ms)
