# Gates: A home dashboard (tree 3, solo)

Scope: a signed-in landing surface at `/home` carrying net worth, the pending-actions carousel,
trending and collections. `/dashboard` is the screener's market dashboard and already redirects to
`/market/today` — a different page for a different question; it is not touched.

Verified before writing these gates (facts, not assumptions):

- `GET /api/v1/cb/pending-actions` + `dismiss` / `resolve` exist (`routers/curated_engage.py:100`),
  and `PendingActionCard` exists (`apps/web/src/components/pending-action-card.tsx`). TRUE — the
  brief's "API and card exist" is right.
- **No** `/home` route anywhere under `apps/web/src/app`. TRUE — the surface does not exist.
- `NetWorthHeader` exists but is rendered only on `/me/investments`.
- `GET /explore/collections` **already exists** (`routers/explore.py:374`) and returns rows; there
  is simply no content in `cb_collection` and nothing renders it. Seeding content is
  `gates/collections.md`'s tree, not this one — home renders the real API with an honest empty
  state.
- **Trending does not exist at all**: no `baskfy_core.curated_trending`, no route, no web module.
  It is built here, as the pure domain + a live-computed read API. The persisted EOD snapshot,
  the Celery Beat job and migration 0020 planned in `gates/trending-root.md` stay open for that
  tree; this one deliberately builds no table and no job. See ABANDON note at the bottom — it is
  a scope boundary, declared, not a silent narrowing.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.
Python CHECKs run from `decile-blueprint/` under `uv run`; web CHECKs from
`decile-blueprint/apps/web` under `pnpm exec`.

Run the checker with the file named explicitly — `gates/` holds other trees' unbuilt plans:

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs GATES.md
```

---

## A — the ranking layer (it has to be real, or the module is a lie)

- [x] G1: `baskfy_core.curated_trending` defines every ranked list once, each with its metric,
      direction, human label and whether it depends on a population. Nine lists, no duplicates.
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.curated_trending import TRENDING_LISTS; ks=[d.key for d in TRENDING_LISTS]; assert len(ks)==len(set(ks)), ks; print(f'LISTS={len(ks)} POP={sum(1 for d in TRENDING_LISTS if d.population_based)}')"
  EXPECT: LISTS=9 POP=3
  EVIDENCE: LISTS=9 POP=3

- [x] G2: House rule 1 holds — the ranking module touches nothing. No database, network, disk or
      clock import anywhere in it.
  CHECK: cd decile-blueprint && grep -nE "^[[:space:]]*(import|from)[[:space:]]+(sqlalchemy|httpx|requests|asyncpg|redis|os|pathlib|open)\b" packages/core/src/baskfy_core/curated_trending.py | wc -l | awk '{print "IO_IMPORTS="$1}'
  EXPECT: IO_IMPORTS=0
  EVIDENCE: IO_IMPORTS=0

- [x] G3: Ranking is deterministic and honest about absent data — ties break on slug, rows whose
      metric is NULL are excluded rather than sorted last, direction is per-list.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_trending.py -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: ........................                                                 [100%] | 24 passed in 0.26s

- [x] G4: The floors are enforced in the pure layer: a list below `MIN_ENTRIES` baskets, or a
      population-based list below `MIN_POPULATION` distinct people, is **withheld with a named
      reason** and never published thin. No fake "most invested" (SC9 AC).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_trending.py -p no:randomly -k "withheld or floor or population" 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: .......                                                                  [100%] | 7 passed, 17 deselected in 0.17s

- [x] G5: `GET /api/v1/cb/trending` answers at the HTTP boundary (not as a bare coroutine),
      requires authentication, and returns every list with its population and withheld reason.
  CHECK: cd decile-blueprint && BASE="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '\"')" && export BASKFY_TEST_DATABASE_URL="${BASE%/*}/baskfy_home_test" BASKFY_REDIS_URL="redis://localhost:6380/5" && uv run pytest services/api/tests/test_curated_trending_http.py -p no:randomly 2>&1 | tail -3
  EXPECT: /\d+ passed/
  EVIDENCE: ..........                                                               [100%] | 10 passed in 8.94s

- [x] G6: The checked-in OpenAPI document and the generated TS client carry the new route — a
      route served and not written down is a surface nobody agreed to. (Scoped: the artifacts
      suite has one failure this tree does not own — `/api/v1/search`, added by a concurrent
      tree and not yet in `EXPECTED_PATHS`. That one test is deselected by name and named here;
      the other 11 of 12 run, including the two that would catch a stale document or a stale
      generated client.)
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_api_artifacts.py -p no:randomly -k "not nothing_undocumented" 2>&1 | tail -2 && grep -c "/cb/trending" packages/api-client/openapi.json packages/api-client/src/generated/schema.ts | tr '\n' ' '
  EXPECT: /openapi\.json:1 .*schema\.ts:1/
  EVIDENCE: ...........                                                              [100%] | packages/api-client/openapi.json:1 packages/api-client/src/generated/schema.ts:1

## B — the surface itself

- [x] G7: `/home` exists inside the authenticated `(app)` group, so the layout's session gate
      covers it, and it is not on the public-path list.
  CHECK: cd decile-blueprint && test -f "apps/web/src/app/(app)/home/page.tsx" && (grep -c '"/home"' apps/web/src/lib/auth/public-routes.ts || true) | awk '{print "PAGE_OK PUBLIC_HITS="$1}'
  EXPECT: PAGE_OK PUBLIC_HITS=0
  EVIDENCE: PAGE_OK PUBLIC_HITS=0 — and it compiles: `next build` reported "Compiled successfully in 2.4min" and emitted `.next/server/app/(app)/home/page.js`, listed in `app-paths-manifest.json`. (That build's *type-check* step then failed inside `.next-e2e/types/`, a stale directory a concurrent session's Playwright run was writing at the time; `pnpm exec tsc --noEmit` over `src/` is clean — see G17.)

- [x] G8: Net worth renders on home from the same payload `/me/investments` reads — one source of
      truth, not a second net-worth computation.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/home src/components/home 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  29 passed (29)

- [x] G9: The pending-actions carousel is dismissible and ends in the terminator card ("That's
      all — no other actions need your attention"); dismiss reaches
      `POST /cb/pending-actions/{id}/dismiss` through a server action, never a browser fetch.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/home/__tests__/pending-actions-carousel.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  6 passed (6)

- [x] G10: The trending module renders on home, every list labelled with what it actually ranks,
      and a withheld list says why in words a person can read.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/home/__tests__/trending-module.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  7 passed (7)

- [x] G11: The collections grid renders on home from `GET /explore/collections` with zero
      hardcoded slugs in the component (SC9 AC), and says so honestly when there are none yet.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/home/__tests__/collections-grid.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  6 passed (6)

- [x] G12: Home degrades instead of throwing: with every upstream read failing, the page still
      renders its four modules in their empty states.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/home/__tests__/fetch.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  10 passed (10)

## C — reachable, and nothing else broken

- [x] G13: Home is a destination a person can reach: it is first in `PRIMARY_NAV` (so both the
      top nav and the bottom tab bar draw it) and the app-shell wordmark points at it.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1 && echo "NAV=$(grep -c 'href: "/home"' src/lib/nav.ts) TAB=$(grep -c 'Home: House' src/components/shell/bottom-tab-bar.tsx) MARK=$(grep -c 'Wordmark href="/home"' src/components/shell/top-nav.tsx)"
  EXPECT: /NAV=1 TAB=1 MARK=1/
  EVIDENCE: Tests  17 passed (17) | NAV=1 TAB=1 MARK=1

- [x] G14: `/home` has a vocabulary entry and `primarySection("/home")` marks the right pill —
      the route table cannot drift from the nav silently.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1 && grep -c '"/home": {' src/lib/vocabulary.ts | awk '{print "VOCAB="$1}'
  EXPECT: VOCAB=1
  EVIDENCE: Tests  17 passed (17) | VOCAB=1

- [x] G15: Nothing sits at a redirected path and no route was shadowed by the new one.
  CHECK: cd decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs 2>&1 | tail -3
  EXPECT: /ok/
  EVIDENCE: ok — 12 redirected route(s) hold a redirect stub and nothing else

- [x] G16: Track C held — no order-shaped affordance appeared on any file this tree owns, and the
      no-order suites still pass.
  CHECK: cd decile-blueprint && echo "EXEC_HITS=$(grep -rnE 'place_order|/execute|OrderGateway|confirm=true' "apps/web/src/app/(app)/home" apps/web/src/components/home apps/web/src/lib/home services/api/src/baskfy_api/routers/curated_trending.py packages/core/src/baskfy_core/curated_trending.py 2>/dev/null | wc -l | tr -d ' ')" && uv run pytest services/api/tests/test_explore_no_orders.py services/api/tests/test_desk_readonly.py -p no:randomly 2>&1 | tail -2
  EXPECT: EXEC_HITS=0
  EVIDENCE: EXEC_HITS=0 | .........                                                                [100%]

- [x] G17: The whole web unit suite passes, the package typechecks with zero errors, and every
      file this tree owns is eslint-clean.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1 && echo "TSC=$(pnpm exec tsc --noEmit 2>&1 | grep -cE '^src/') ESLINT=$(pnpm exec eslint "src/app/(app)/home" src/app/actions/pending-actions.ts src/components/home src/lib/home src/lib/nav.ts src/lib/vocabulary.ts src/components/shell/wordmark.tsx src/components/shell/top-nav.tsx src/components/shell/bottom-tab-bar.tsx src/components/investments/net-worth-header.tsx e2e/nav.spec.ts >/dev/null 2>&1 && echo ok || echo bad)"
  EXPECT: /TSC=0 ESLINT=ok/
  EVIDENCE: Tests  1471 passed (1471) | TSC=0 ESLINT=ok

- [x] G18: The Python side is green and clean — new suites pass, siblings still pass, and every
      file this tree owns is ruff / ruff-format / mypy clean.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && bash gates/home-python.sh 2>&1 | tail -4
  EXPECT: PY_OK
  EVIDENCE: suites: 85 passed | PY_OK

- [x] G19: The judgement calls are recorded where this repo records them, tagged UNREVIEWED, and
      the status page says what is now done and what is still not.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "HOME=$(grep -cE '^## HOME[0-9]' docs/DECISIONS-MERGE.md) TAGGED=$(grep -E '^## HOME[0-9]' docs/DECISIONS-MERGE.md | grep -c UNREVIEWED) STATUS=$(grep -c '/home' docs/smallcase/STATUS.md)"
  EXPECT: /HOME=([1-9][0-9]*) TAGGED=\1 STATUS=[1-9]/
  EVIDENCE: HOME=3 TAGGED=3 STATUS=8

- [x] G20: The e2e route table knows about `/home` — the nav spec walks every primary destination
      and this one is in it, and the spec gained a landing test, a wordmark test and a
      "/dashboard still goes to Market" test. **The browser suite was not executed** (Playwright
      needs the app, the API and a database that other sessions held all sitting), so this gate
      proves the route table and the lint, not a green Playwright run. Said again in the report.
  CHECK: cd decile-blueprint/apps/web && grep -c '"/home"' e2e/nav.spec.ts src/lib/nav.ts | tr '\n' ' '
  EXPECT: /nav\.ts:[1-9]/
  EVIDENCE: e2e/nav.spec.ts:1 src/lib/nav.ts:2

- [x] G21: Report audit — every number in the final report was re-measured at report time, not
      written from memory, and the ledger count is pasted.
  EVIDENCE: re-measured at report time — core `24 passed`, trending HTTP `10 passed`, the G18
    script's seven suites `85 passed`, home+nav web `46 passed (46)`, whole web suite
    `1471 passed (1471)` across `81` files, `tsc --noEmit` `0` lines of error output, artifacts
    `11 passed, 1 deselected`, no-order `9 passed`, `LISTS=9 POP=3`, `HOME=3 TAGGED=3`, 17 files
    created + 11 modified. Two numbers written earlier were wrong and are corrected in place:
    the artifacts suite is 12 tests, not 21 (G6's text), and `apps/web` typechecks at 0 errors
    now, not the 4 measured mid-run (STATUS.md) — a concurrent tree closed them.

<!--
ABANDON lines, if any, go here.
-->
ABANDON: SNAPSHOT the persisted EOD trending snapshot (migration 0020, `cb_trending_snapshot`, the Celery Beat job `baskfy.cb.compute_trending`) is deliberately NOT built here. `/cb/trending` computes live from `cb_basket` / `cb_metrics` / `cb_watchlist_item` / `cb_investment` / `cb_order_batch` on request, which is correct for a catalog this size and keeps the change reversible. `gates/trending-root.md` G5/G6/G9 remain open for the tree that owns persistence; the pure domain module it names (`baskfy_core.curated_trending`) is built here so that tree extends rather than competes.

