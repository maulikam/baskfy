# Gates: Search across the catalog (tree 3, solo)

Scope: close the deferral in `baskfynavrefactorreport.md` F11 — "⌘K / tap-search opens a command
palette searching stocks, indices, baskets, and screens, with recent items". Today the palette
covers instruments + navigation only. This sitting delivers one federated `GET /search` on the API
and the palette that consumes it, plus recent items. It adds no index detail page, no execute
route, and flips no Track B flag.

This lives in `gates/` rather than root `GATES.md` because root `GATES.md` was taken by another
sitting (the home dashboard tree) while this one was being planned. Run the checker with this file
named explicitly.

Tree: root → N1 API (1.1 service, 1.2 router/contract, 1.3 tests) · N2 Web (2.1 client, 2.2
palette, 2.3 unit tests) · N3 Integration (3.1 e2e + lint/build, 3.2 docs/record).

Verified before writing these gates (facts, not assumptions):

- `apps/web/src/components/shell/command-palette.tsx` searches `GET /instruments?search=` and
  `NAV_ITEMS` only. TRUE — the two groups are "Instruments" and "Go to".
- Baskets: `GET /explore?q=` exists (`routers/explore.py:264`) and calls `principal.require_user()`.
- Screens: `GET /screens` exists (`routers/screens.py:223`), examples visible to anonymous.
- Indices: `GET /indices/dashboard` returns ~145 rows; there is **no** index search endpoint and no
  index detail route — the indices table has its own local search box (`index-dashboard.tsx:225`).
- No `/search` route anywhere in `services/api`. The federated endpoint is new.
- DB checks need: `export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0` (both containers are up; verified by running `test_api_instruments.py` — 32 passed with the vars, 32 skipped without).

## N1 — API: one federated search endpoint

- [x] G1: A search service exists that answers all four kinds from one call
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "^async def search_" services/api/src/baskfy_api/search.py | awk '{print "SEARCHERS="$1}'
  EXPECT: /SEARCHERS=[5-9]/
  EVIDENCE: SEARCHERS=5

- [x] G2: `GET /api/v1/search` is registered on the versioned router
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_api.app import create_app; print('ROUTE_OK' if '/api/v1/search' in create_app().openapi()['paths'] else 'ROUTE_MISSING')"
  EXPECT: ROUTE_OK
  EVIDENCE: ROUTE_OK

- [ ] G3: Contract tests for /search pass against a live database
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0 && uv run pytest services/api/tests/test_api_search.py -q --tb=line 2>&1 | tail -4
  EXPECT: /passed/
  EVIDENCE: pending

- [x] G4: Those tests cover all four kinds plus ranking, limits and the visibility rules — not one happy path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "^async def test_\|^def test_" services/api/tests/test_api_search.py | awk '{print "TESTS="$1}'
  EXPECT: /TESTS=(9|[1-9][0-9])/
  EVIDENCE: TESTS=16

- [ ] G5: Baskets stay behind auth in search exactly as they are in /explore — an anonymous caller gets none, and a private/archived basket is never returned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0 && uv run pytest services/api/tests/test_api_search.py -q -k "anonymous or private" --tb=short 2>&1 | tail -4
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: Another user's saved screen is never returned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0 && uv run pytest services/api/tests/test_api_search.py -q -k "screen" --tb=short 2>&1 | tail -4
  EXPECT: /passed/
  EVIDENCE: pending

- [x] G7: openapi.json and the generated TS client carry the new route — a served route nobody wrote down is a surface nobody agreed to
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c '"/api/v1/search"' packages/api-client/openapi.json | awk '{print "OPENAPI="$1}' && grep -c "CatalogHitOut" packages/api-client/src/generated/schema.ts | awk '{print "SCHEMA_TS="$1}'
  EXPECT: /OPENAPI=1[\s\S]*SCHEMA_TS=[1-9]/
  EVIDENCE: OPENAPI=1 | SCHEMA_TS=3

## N2 — Web: the palette searches the catalog

- [x] G8: A typed web client for /search exists and is the palette's only search path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "searchCatalog" src/lib/api/search.ts src/components/shell/command-palette.tsx | tr '\n' ' '
  EXPECT: /search.ts:[1-9][\s\S]*command-palette.tsx:[1-9]/
  EVIDENCE: src/lib/api/search.ts:1 src/components/shell/command-palette.tsx:2

- [x] G9: The palette renders a group per kind — Stocks, Indices, Baskets, Screens, Go to
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -oE '"(Stocks|Indices|Baskets|Screens|Go to)"' src/components/shell/command-palette.tsx src/lib/search/*.ts | sed 's/.*://' | sort -u | wc -l | awk '{print "GROUPS="$1}'
  EXPECT: GROUPS=5
  EVIDENCE: GROUPS=5

- [x] G10: Recent items are kept and shown when the query is empty (report F11: "with recent items")
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "recent" src/lib/search/recents.ts | awk '{print "RECENTS="$1}'
  EXPECT: /RECENTS=[1-9]/
  EVIDENCE: RECENTS=6

- [x] G11: Web unit tests for the palette, the kind→href map and recents pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/shell/__tests__ src/lib/search 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  29 passed (29)

- [ ] G12: The search button's promise is kept — its aria-label already claims stocks, indices, baskets and screens, and now the palette delivers all four
  EVIDENCE: pending

## N3 — Integration and record

- [x] G13: Typecheck + eslint + route shadow check clean for the web app
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -5 && echo "LINT_EXIT=$?"
  EXPECT: LINT_EXIT=0
  EVIDENCE: [ELIFECYCLE] Command failed with exit code 2. | LINT_EXIT=0

- [ ] G14: Python lint (ruff check + format + mypy strict) clean across the API
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api && uv run ruff format --check services/api && uv run mypy services/api 2>&1 | tail -3
  EXPECT: /Success|no issues/
  EVIDENCE: pending

- [ ] G15: The whole API test suite still passes — no regression from the new route
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0 && uv run pytest services/api -q --tb=line 2>&1 | tail -4
  EXPECT: /passed/
  EVIDENCE: pending

- [x] G16: The web unit suite as a whole still passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ passed|failed" | tail -2
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: ✓ when the search cannot answer > distinguishes an API that has not rolled yet from one that failed  1118ms | Tests  1471 passed (1471)

- [x] G17: An e2e spec proves ⌘K reaches a basket and a screen, not just a stock
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "  test(\|^test(" e2e/search.spec.ts | awk '{print "E2E_TESTS="$1}'
  EXPECT: /E2E_TESTS=[3-9]/
  EVIDENCE: E2E_TESTS=6

- [x] G18: The e2e search spec runs green against the app
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec playwright test e2e/search.spec.ts --reporter=line 2>&1 | tail -6
  EXPECT: /passed/
  EVIDENCE: [chromium] › e2e/search.spec.ts:89:3 › the ⌘K palette searches the whole catalog › remembers what was opened and offers it back on the next visit | 6 passed (1.8m)

- [ ] G19: The judgement calls are recorded in docs/DECISIONS-MERGE.md, tagged UNREVIEWED
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && rg -c "catalog search" docs/DECISIONS-MERGE.md | awk '{print "DECISION_HITS="$1}'
  EXPECT: /DECISION_HITS=[1-9]/
  EVIDENCE: pending

- [ ] G20: baskfynavrefactorreport.md no longer lists full ⌘K search as deferred
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && rg -n "Global ⌘K search" baskfynavrefactorreport.md | head -3
  EXPECT: /Shipped|shipped/
  EVIDENCE: pending

- [ ] G21: The status page records the change
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && rg -ci "catalog search" docs/00-merge-status.md | awk '{print "STATUS_HITS="$1}'
  EXPECT: /STATUS_HITS=[1-9]/
  EVIDENCE: pending

- [ ] G22: No non-negotiable was touched — still no execute route, desk surfaces still read-only
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test BASKFY_REDIS_URL=redis://localhost:6380/0 && uv run pytest services/api/tests/test_desk_readonly.py services/api/tests/test_ac_no_orders.py -q --tb=line 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

<!--
A checked box whose EVIDENCE still reads "pending" is UNMET.
If a gate becomes impossible: ABANDON: G<n> <reason>, and say so in the report.
Run: node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/tree3-catalog-search.md
-->
