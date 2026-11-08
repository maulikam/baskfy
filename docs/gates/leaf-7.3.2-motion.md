# Gates: 7.3.2 Motion: FLIP on sort, stagger on load

Scope: the row layer of `components/data/data-table.tsx` + any keyframes in `globals.css`.

Measured gap: §3.2 is absent. Rows currently share one uniform fade; there is no FLIP and no
stagger. No new dependency is allowed (`docs/02` locks the stack — no framer-motion), so this is
CSS plus `getBoundingClientRect`.

- [x] G1: When a sort flips, rows animate from their previous position to their new one rather
      than snapping.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "flip" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:56 | Duration  2.26s (transform 121ms, setup 156ms, collect 309ms, tests 318ms, environment 1.06s, prepare 68ms)

- [x] G2: Rows stagger in on load/re-filter at 15–20ms per row, and the stagger is capped so row
      271 does not wait seconds.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "stagger" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:00 | Duration  2.28s (transform 103ms, setup 267ms, collect 292ms, tests 237ms, environment 1.10s, prepare 92ms)

- [x] G3: Under `prefers-reduced-motion: reduce` nothing animates — no FLIP, no stagger.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "reduced" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:03 | Duration  2.07s (transform 103ms, setup 213ms, collect 346ms, tests 131ms, environment 956ms, prepare 118ms)

- [x] G4: No animation exceeds 300ms, and only transform/opacity are animated (the repo's motion
      budget).
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/table-motion.test.tsx -t "G4" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:06 | Duration  2.50s (transform 91ms, setup 173ms, collect 252ms, tests 638ms, environment 1.03s, prepare 153ms)

- [x] G5: No new runtime dependency was added.
  CHECK: cd decile-blueprint/apps/web && c=$(node -e 'const d=require("./package.json").dependencies||{};console.log(Object.keys(d).filter(k=>/framer-motion|gsap|react-spring|animejs|popmotion/.test(k)).length)'); echo "animation-deps=$c"
  EXPECT: animation-deps=0
  EVIDENCE: animation-deps=0

- [x] G6: Virtualised scrolling still works and the sticky header still sticks — FLIP on a
      virtualised list is where this breaks.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/data/__tests__/ 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:47 | Duration  1.14s (transform 165ms, setup 493ms, collect 708ms, tests 699ms, environment 2.19s, prepare 250ms)
