# Gates: 7.4.1 The mobile card feed is not 271 nodes

Scope: `components/screens/result-cards.tsx`.

Measured defect: at 390px the grid is hidden and the card list renders all 271 rows into the DOM.

- [ ] G1: The card list renders a bounded number of nodes for a 271-row result.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-cards.test.tsx 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: pending

- [ ] G2: Scrolling still reaches the last row, and tapping a card still opens the drawer.
  EVIDENCE: pending

- [ ] G3: Each card keeps its rank badge, symbol/name, score bar and return chip (§3.5).
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/result-cards.test.tsx -t "card" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: pending
