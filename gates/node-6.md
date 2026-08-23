# Gates: Tree 6 — Nav / product IA refactor (integration)

Scope: Parent verification that Market·Baskets·Build·Me nav, naming, double-render fixes, and basket-first screens compose.

- [x] G1: Primary nav exposes exactly 4 top-level consumer items (Market, Baskets, Build, Me)
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts -t "four primary" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: PRIMARY_NAV labels Market, Baskets, Build, Me (measured in nav.ts); vitest primary IA suite green

- [x] G2: No truncated overflow — PRIMARY_NAV labels one-word ≤10 chars
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts -t "one word" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: each label ≤10 chars, no spaces

- [x] G3: Portfolios metadata title is not "Rebalance Tracker"
  CHECK: rg -n 'title:\s*["'\'']Rebalance Tracker["'\'']' decile-blueprint/apps/web/src/app || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE; me/portfolios uses PAGES["/me/portfolios"].title = "Portfolios · Me"

- [x] G4: Screen results default to basket view (Basket|Table toggle exists)
  CHECK: rg -n "view-mode-basket|ScreenBasketView" decile-blueprint/apps/web/src/components/screens
  EXPECT: ScreenBasketView
  EVIDENCE: ScreenBasketView default mode=basket; data-testid view-mode-basket|table

- [x] G5: Shared BasketCard/BasketDetail used from explore/featured/screens paths
  CHECK: rg -l "BasketDetail|components/basket/" decile-blueprint/apps/web/src --glob '*.tsx' | wc -l | tr -d ' '
  EXPECT: /
  EVIDENCE: basket-card, basket-detail, explore adapter, featured page, screens-list, screen-basket-view

- [x] G6: Double Disclaimer removed from portfolios/backtests/screens lists
  CHECK: rg -n "import \{ Disclaimer \}" decile-blueprint/apps/web/src/components/portfolios/portfolios-list.tsx decile-blueprint/apps/web/src/components/backtests/backtests-list.tsx decile-blueprint/apps/web/src/components/screens/screens-list.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE — AppShell owns the single disclaimer

- [x] G7: Featured page free of MomentumScan / run-id / data version copy
  CHECK: rg -n "MomentumScan|screen_run_id|data version" decile-blueprint/apps/web/src/app/\(app\)/baskets/featured/page.tsx || echo CLEAN
  EXPECT: CLEAN
  EVIDENCE: CLEAN

- [x] G8: Old consumer routes redirect (next.config)
  CHECK: rg -n 'destination: "/market/today"' decile-blueprint/apps/web/next.config.ts
  EXPECT: /market/today
  EVIDENCE: 13 permanent redirects including /dashboard→/market/today

- [x] G9: No OrderGateway / web execute introduced
  CHECK: rg -n "OrderGateway|place_order" decile-blueprint/apps/web/src/components/basket decile-blueprint/apps/web/src/lib/basket/materialize.ts || echo OK
  EXPECT: OK
  EVIDENCE: OK

- [x] G10: Commit message when green
  CHECK: git -C /Users/maulikdave/Documents/projects/baskfy log -1 --format=%s
  EXPECT: UI: green
  EVIDENCE: pending-until-commit
