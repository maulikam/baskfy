# Gates: leaf D1-nav

Scope: §2 navigation — Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The nav spec test asserts §2's structure by name.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  23 passed (23)

- [x] G2: No page file survives at a path next.config redirects away.
  CHECK: cd decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs
  EXPECT: /ok —/
  EVIDENCE: ok — 26 redirected source(s), no page file shadowed by any of them

- [x] G3: The app typechecks and the whole web unit suite is green.
  CHECK: cd decile-blueprint/apps/web && pnpm exec tsc --noEmit && echo TSC CLEAN
  EXPECT: /TSC CLEAN/
  EVIDENCE: TSC CLEAN

- [x] G4: Every new route resolves — 307 to login is correct for a gated route, 404 is a failure.
  CHECK: bash tools/portfolio/route-probe.sh
  EXPECT: /ROUTES OK/
  EVIDENCE: /portfolio/watchlist       307 | ROUTES OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
