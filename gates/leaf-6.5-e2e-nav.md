# Gates: leaf-6.5 — Nav e2e + consumer jargon polish

Scope: Playwright coverage for Tree 6 IA; legacy redirect table; remaining "data version" leaks on consumer chrome.

- [x] G1: `LEGACY_REDIRECTS` registry matches thirteen permanent redirects
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts -t "thirteen legacy" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: nav.test.ts LEGACY_REDIRECTS length 13

- [x] G2: Nav e2e spec covers four-item IA at 768/1024/1280 and mobile tabs
  CHECK: rg -n "768, 1024, 1280|bottom-tab-bar|PRIMARY_NAV" decile-blueprint/apps/web/e2e/nav.spec.ts
  EXPECT: viewport loop + bottom-tab-bar
  EVIDENCE: e2e/nav.spec.ts

- [x] G3: Legacy redirect e2e walks `LEGACY_REDIRECTS`
  CHECK: rg -n "LEGACY_REDIRECTS" decile-blueprint/apps/web/e2e/nav.spec.ts
  EXPECT: for-loop over LEGACY_REDIRECTS
  EVIDENCE: e2e/nav.spec.ts legacy consumer routes block

- [x] G4: Example screen defaults to basket view (view-mode-basket pressed)
  CHECK: rg -n "view-mode-basket|Investing 001" decile-blueprint/apps/web/e2e/nav.spec.ts
  EXPECT: view-mode-basket
  EVIDENCE: e2e/nav.spec.ts screen results materialize block

- [x] G5: Consumer chrome drops user-visible "data version"
  CHECK: rg -n "data version" decile-blueprint/apps/web/src/components/shell/freshness-pill.tsx decile-blueprint/apps/web/src/components/portfolios/buffer-explainer.tsx || echo CLEAN
  EXPECT: CLEAN
  EVIDENCE: CLEAN — tooltip and buffer explainer use plain dates only

- [x] G6: nav + jargon + materialize vitest green
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts src/lib/__tests__/jargon-ban.test.ts src/lib/basket/__tests__/materialize.test.ts 2>&1 | tail -10
  EXPECT: passed
  EVIDENCE: 21 tests passed (14 nav + 5 jargon + 2 materialize)

- [ ] G7: Full nav e2e against production build
  CHECK: cd decile-blueprint/apps/web && pnpm run e2e -- e2e/nav.spec.ts 2>&1 | tail -20
  EXPECT: passed OR ABANDON with reason
  EVIDENCE: pending — requires `make up` + baskfy_e2e DB
