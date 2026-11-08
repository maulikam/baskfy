# Gates: leaf-6.2 — Routes, redirects, title normalization

Scope: Canonical Market/Baskets/Build/Me routes; permanent redirects from old paths; title = nav label.

- [x] G1: next.config permanent redirects for legacy consumer paths
  CHECK: rg -c "permanent: true" decile-blueprint/apps/web/next.config.ts
  EXPECT: 13
  EVIDENCE: 13 permanent redirects (dashboard, market-health, listings, explore, screens×3, backtests×2, investments×2, portfolios, watchlist)

- [x] G2: Vocabulary PAGES entries for canonical hubs
  CHECK: rg -n '"/market/today"|"/build"|"/me/investments"|"/baskets/featured"' decile-blueprint/apps/web/src/lib/vocabulary.ts
  EXPECT: /market/today
  EVIDENCE: PAGES keys present for all Tree 6 hubs

- [x] G3: portfolios metadata uses PAGES title (not hardcoded Rebalance Tracker)
  CHECK: rg -n "Rebalance Tracker" decile-blueprint/apps/web/src/app --glob '**/page.tsx' | rg -v formerly | rg -v 'was `/portfolios`' || echo NONE
  EXPECT: NONE
  EVIDENCE: me/portfolios uses PAGES["/me/portfolios"].title; no title: "Rebalance Tracker"

- [x] G4: Section tab layouts exist for Market and Me
  CHECK: rg -l "SectionTabs" decile-blueprint/apps/web/src | wc -l | tr -d ' '
  EXPECT: /
  EVIDENCE: section-tabs.tsx + market/me/baskets/build pages
