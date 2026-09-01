# Gates: A — one investments journey

Scope: `/investments/[id]/{customize,orders}` and `/me/investments/[id]/{customize,orders,costs}`
are both live. Keep `/me/*` (Tree 6's canonical section), retire the legacy tree by redirect.

- [x] A1: The legacy tree serves no page component of its own — every legacy path redirects.
  CHECK: bash gates/tree4-check-routes.sh 2>&1 | grep -E "LEGACY_PAGE_BODIES|ONE_JOURNEY"
  EXPECT: LEGACY_PAGE_BODIES=0
  EVIDENCE: LEGACY_PAGE_BODIES=0 — src/app/(app)/investments removed. It held 3 page bodies: [id]/page.tsx (181 lines), [id]/orders (79), [id]/customize (57).

- [x] A2: Every legacy path redirects to its `/me` twin, preserving the id — including the two
      deep paths the brief names.
  CHECK: bash gates/tree4-check-routes.sh 2>&1 | grep -E "^REDIRECT "
  EXPECT: /REDIRECT \/investments\/9\/orders -> \/me\/investments\/9\/orders/
  EVIDENCE: REDIRECT /investments -> /me/investments · /investments/9 -> /me/investments/9 · /investments/9/orders -> /me/investments/9/orders · /investments/9/customize -> /me/investments/9/customize · /investments/9/costs -> /me/investments/9/costs. All 308.

- [x] A3: The redirect is permanent and registered where the other legacy redirects live, not
      invented per-file.
  EVIDENCE: Already registered, not invented: next.config.ts redirects() carries {source:'/investments/:path*', destination:'/me/investments/:path*', permanent:true}, mirrored in src/lib/nav.ts LEGACY_REDIRECTS lines 175-176. This tree added no new redirect — it removed the dead pages the redirect already shadowed.

- [x] A4: No link in the app still points at the legacy tree.
  CHECK: cd decile-blueprint/apps/web && rg -n 'href="/investments|href={`/investments' src -g '*.tsx' | rg -v '__tests__' | wc -l | awk '{print "APP_LINKS_TO_LEGACY="$1}'
  EXPECT: APP_LINKS_TO_LEGACY=0
  EVIDENCE: APP_LINKS_TO_LEGACY=0, down from 13 occurrences across 5 files inside the canonical /me tree. Check proven able to fail: injecting one legacy href gave APP_LINKS_TO_LEGACY=1 / ONE_JOURNEY_FAIL, removing it restored 0 / ONE_JOURNEY_OK.
