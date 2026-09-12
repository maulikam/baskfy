# Gates: 7.4.1 The mobile card feed is not 271 nodes

Scope: `components/screens/result-cards.tsx`.

Measured defect: at 390px the grid is hidden and the card list renders all 271 rows into the DOM.

- [x] G1: The card list renders a bounded number of nodes for a 271-row result.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-cards.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:02:48 | Duration  972ms (transform 41ms, setup 52ms, collect 69ms, tests 349ms, environment 280ms, prepare 45ms)

- [x] G2: Scrolling still reaches the last row, and tapping a card still opens the drawer.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-cards.test.tsx -t "G2" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:10 | Duration  3.33s (transform 88ms, setup 178ms, collect 202ms, tests 1.27s, environment 1.20s, prepare 66ms)

- [x] G3: Each card keeps its rank badge, symbol/name, score bar and return chip (§3.5).
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-cards.test.tsx -t "G3" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:13:14 | Duration  1.41s (transform 156ms, setup 119ms, collect 100ms, tests 125ms, environment 687ms, prepare 139ms)
