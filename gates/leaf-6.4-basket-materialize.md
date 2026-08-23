# Gates: leaf-6.4 — BasketCard/BasketDetail + screen materialization

Scope: Shared basket components; screen run/save materializes as investable basket; table behind toggle.

- [x] G1: shared basket components exist
  CHECK: ls decile-blueprint/apps/web/src/components/basket/basket-detail.tsx decile-blueprint/apps/web/src/components/basket/basket-card.tsx
  EXPECT: basket-detail.tsx
  EVIDENCE: both files present

- [x] G2: materializeBasket builds equal-weight + 5% cash
  CHECK: rg -n "materializeBasket|DEFAULT_CASH_BUFFER" decile-blueprint/apps/web/src/lib/basket/materialize.ts
  EXPECT: materializeBasket
  EVIDENCE: DEFAULT_CASH_BUFFER=0.05; equal-weight mode default

- [x] G3: ResultsPanel defaults to basket view with toggle
  CHECK: rg -n "ScreenBasketView|view-mode-basket" decile-blueprint/apps/web/src/components/screens
  EXPECT: ScreenBasketView
  EVIDENCE: results-panel wraps ScreenBasketView; default mode basket

- [x] G4: Unit tests for materialize + nav pass
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/basket/__tests__/materialize.test.ts src/lib/__tests__/nav.test.ts 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: 15 tests passed (2+13)

- [x] G5: /build lists basket cards; /baskets Auto section
  CHECK: rg -n "Auto — from your screens" decile-blueprint/apps/web/src --glob '*.tsx'
  EXPECT: Auto — from your screens
  EVIDENCE: baskets/page.tsx + screens-list ScreenCard badge
