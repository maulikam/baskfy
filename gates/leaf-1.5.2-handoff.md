# Gates: leaf-1.5.2-handoff

Scope: PlanHandoffPanel + MarketClosedModal; no order button

- [x] G1: handoff component exists
  CHECK: rg -l 'PlanHandoff|MarketClosed' decile-blueprint/apps/web/src/components -g '*.tsx' | head -1
  EXPECT: tsx
  EVIDENCE: decile-blueprint/apps/web/src/components/cb/plan-handoff-panel.tsx (+ market-closed-modal.tsx; wired from investments/investment-actions.tsx and explore/invest-cta.tsx)

<!-- integrity: security, performance, memory, accuracy required -->
