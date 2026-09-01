# Gates: Search across the catalog (tree 3, solo)

Scope: close the deferral in `baskfynavrefactorreport.md` F11 — "⌘K / tap-search opens a command
palette searching stocks, indices, baskets, and screens, with recent items". The palette covered
instruments + navigation only. This sitting delivers one federated `GET /search` and the palette
that consumes it, plus recent items. It adds no index detail page, no execute route, and flips no
Track B flag.

This lives in `gates/` rather than root `GATES.md` because root `GATES.md` was taken by another
sitting (the home-dashboard tree) minutes after this one was planned. Run the checker with this
file named explicitly.

Tree: root → N1 API (1.1 service, 1.2 router/contract, 1.3 tests) · N2 Web (2.1 client, 2.2
palette, 2.3 unit tests) · N3 Integration (3.1 e2e + lint/build, 3.2 docs/record).

Verified before writing these gates (facts, not assumptions):

- `apps/web/src/components/shell/command-palette.tsx` searched `GET /instruments?search=` and
  `NAV_ITEMS` only. TRUE — the two groups were "Instruments" and "Go to".
- Baskets: `GET /explore?q=` exists (`routers/explore.py:264`) and calls `principal.require_user()`.
- Screens: `GET /screens` exists (`routers/screens.py:223`), examples visible to anonymous.
- Indices: `GET /indices/dashboard` returns ~145 rows; there is **no** index search endpoint and no
  index detail route — the indices table has its own local search box (`index-dashboard.tsx:225`).
- No `/search` route anywhere in `services/api`. The federated endpoint is new.

**This machine is shared with other Claude sessions working the same tree.** Two consequences the
CHECKs below are written around, both proven rather than assumed:

- The DB-backed suites use a private database, `baskfy_test_m40`, and Redis db 2. A concurrent run
  against `baskfy_test` drops the schema under this one — measured: 769 setup ERRORs on the shared
  database, 5 failures on the private one, same code.
- `pnpm run lint`'s eslint half and the whole-suite pytest run are shared surfaces with
  pre-existing failures owned by other trees. Those gates are scoped to what this sitting owns and
  say so, rather than being declared green over someone else's red.

## N1 — API: one federated search endpoint

- [x] G1: A search service exists that answers all four kinds from one call
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "^async def search_" services/api/src/baskfy_api/search.py | awk '{print "SEARCHERS="$1}'
  EXPECT: /SEARCHERS=[5-9]/
  EVIDENCE: SEARCHERS=5

- [x] G2: `GET /api/v1/search` is served and documented
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_api.app import create_app; print('ROUTE_OK' if '/api/v1/search' in create_app().openapi()['paths'] else 'ROUTE_MISSING')"
  EXPECT: ROUTE_OK
  EVIDENCE: ROUTE_OK

- [x] G3: Contract tests for /search pass against a live database — asserted by count, so a fully skipped run cannot pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api/tests/test_api_search.py -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ (passed|failed|skipped|error)" | tr '\n' ' '
  EXPECT: /^17 passed /
  EVIDENCE: 17 passed

- [x] G4: Those tests cover all four kinds plus ranking, limits and the visibility rules — not one happy path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c "^async def test_\|^def test_" services/api/tests/test_api_search.py | awk '{print "TESTS="$1}'
  EXPECT: /TESTS=(9|[1-9][0-9])/
  EVIDENCE: TESTS=17

- [x] G5: Baskets stay behind auth in search exactly as they are in /explore — an anonymous caller gets none, and a PRIVATE basket is never returned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api/tests/test_api_search.py -k "anonymous or private" -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ passed|[0-9]+ failed" | tr '\n' ' '
  EXPECT: /^4 passed/
  EVIDENCE: 4 passed

- [x] G6: Another user's saved screen is never returned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api/tests/test_api_search.py -k "another_users_screen" -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ passed|[0-9]+ failed" | tr '\n' ' '
  EXPECT: /^1 passed/
  EVIDENCE: 1 passed

- [x] G7: openapi.json and the generated TS client carry the new route — a served route nobody wrote down is a surface nobody agreed to
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -c '"/api/v1/search"' packages/api-client/openapi.json | awk '{print "OPENAPI="$1}' && grep -c "CatalogHitOut" packages/api-client/src/generated/schema.ts | awk '{print "SCHEMA_TS="$1}'
  EXPECT: /OPENAPI=1[\s\S]*SCHEMA_TS=[1-9]/
  EVIDENCE: OPENAPI=1 | SCHEMA_TS=3

- [x] G7b: The route is in `test_api_artifacts.EXPECTED_PATHS`, and that suite is green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api/tests/test_api_artifacts.py -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ passed|[0-9]+ failed" | tr '\n' ' '
  EXPECT: /^12 passed/
  EVIDENCE: 12 passed

## N2 — Web: the palette searches the catalog

- [x] G8: A typed web client for /search exists and is the palette's only search path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "searchCatalog" src/lib/api/search.ts src/components/shell/command-palette.tsx | tr '\n' ' '
  EXPECT: /search.ts:[1-9][\s\S]*command-palette.tsx:[1-9]/
  EVIDENCE: src/lib/api/search.ts:1 src/components/shell/command-palette.tsx:2

- [x] G9: The palette renders a group per kind — Stocks, Indices, Baskets, Screens, Go to
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -ohE '"(Stocks|Indices|Baskets|Screens|Go to)"' src/components/shell/command-palette.tsx src/lib/search/hrefs.ts | sort -u | wc -l | awk '{print "GROUPS="$1}'
  EXPECT: GROUPS=5
  EVIDENCE: GROUPS=5

- [x] G10: Recent items are kept and shown when the query is empty (report F11: "with recent items")
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "recent" src/lib/search/recents.ts | awk '{print "RECENTS="$1}'
  EXPECT: /RECENTS=[1-9]/
  EVIDENCE: RECENTS=6

- [x] G11: Web unit tests for the palette, the kind→href map and recents pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/shell/__tests__ src/lib/search 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +31 passed \(31\)/
  EVIDENCE: Tests  31 passed (31)

- [x] G12: The search button's promise is kept — its aria-label already claimed all four kinds, and the palette now delivers them
  EVIDENCE: `search-button.tsx:18` reads aria-label="Search stocks, indices, baskets, and screens" — written in M36 when only instruments were searchable. `e2e/search.spec.ts` "one query reaches three kinds at once" now clicks through Indices, Screens and Baskets groups from one query, and the fourth (Stocks) has its own passing e2e case. The label stopped being a promise and became a description.

## N3 — Integration and record

- [x] G13: The web app typechecks — whole app, zero errors
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit 2>&1 | grep -c "error TS" | awk '{print "TSC_ERRORS="$1}'
  EXPECT: TSC_ERRORS=0
  EVIDENCE: TSC_ERRORS=0

- [x] G13b: eslint is clean for every file this sitting touched
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec eslint src/components/shell/command-palette.tsx src/components/shell/__tests__/command-palette.test.tsx src/lib/search src/lib/api/search.ts src/test/local-storage.ts e2e/search.spec.ts e2e/keyboard.spec.ts > /tmp/m40-eslint.txt 2>&1; grep -c "error" /tmp/m40-eslint.txt | awk '{print "MINE_ESLINT_ERRORS="$1}'
  EXPECT: MINE_ESLINT_ERRORS=0
  EVIDENCE: MINE_ESLINT_ERRORS=0

- [x] G14: ruff + ruff format + mypy strict clean for every Python file this sitting touched
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && FILES="services/api/src/baskfy_api/search.py services/api/src/baskfy_api/routers/search.py services/api/tests/test_api_search.py services/api/tests/test_api_artifacts.py services/api/src/baskfy_api/schemas.py services/api/src/baskfy_api/app.py services/api/src/baskfy_api/seed.py packages/core/src/baskfy_core/backtest.py" && uv run ruff check $FILES && uv run ruff format --check $FILES && uv run mypy $FILES 2>&1 | tail -1
  EXPECT: /Success: no issues found in 8 source files/
  EVIDENCE: 8 files already formatted | Success: no issues found in 8 source files

- [x] G14b: House rule 3 still holds repo-wide — no `# type: ignore`, no `any`, no swallowed exceptions
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_no_escape_hatches.py -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ passed|[0-9]+ failed" | tr '\n' ' '
  EXPECT: /^8 passed/
  EVIDENCE: 8 passed

- [x] G15: Every API-suite failure is one of the five pre-existing ones owned by other trees — nothing this sitting touched fails, and nothing new appears
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api --tb=no 2>&1 | grep "^FAILED" | sed 's/^FAILED //;s/::.*//;s|services/api/tests/||' | sort -u > /tmp/m46-failing.txt; printf 'test_api_run.py\ntest_baskets_readonly.py\ntest_curated_schema.py\ntest_load.py\ntest_seed.py\n' > /tmp/m46-known.txt; echo "UNEXPECTED=[$(comm -23 /tmp/m46-failing.txt /tmp/m46-known.txt | tr '\n' ' ')] OF=$(wc -l < /tmp/m46-failing.txt | tr -d ' ')"
  EXPECT: /UNEXPECTED=\[\] OF=[0-5]/
  EVIDENCE: UNEXPECTED=[] OF=1

- [x] G16: The web unit suite as a whole still passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +1473 passed \(1473\)/
  EVIDENCE: Tests  1473 passed (1473)

- [x] G17: An e2e spec proves ⌘K reaches a basket and a screen, not just a stock
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c "^  test(" e2e/search.spec.ts | awk '{print "E2E_TESTS="$1}'
  EXPECT: /E2E_TESTS=[3-9]/
  EVIDENCE: E2E_TESTS=6

- [x] G18: The e2e search spec runs green against the built app
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec playwright test e2e/search.spec.ts --reporter=line 2>&1 | grep -E "^ +[0-9]+ (passed|failed)" | tr '\n' ' '
  EXPECT: /7 passed/
  EVIDENCE: 7 passed (31.4s)

- [x] G19: The judgement calls are recorded in docs/DECISIONS-MERGE.md, tagged UNREVIEWED
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -cE "^### M46\.[0-9]|^## M46 " docs/DECISIONS-MERGE.md | awk '{print "M46_SECTIONS="$1}'
  EXPECT: /M46_SECTIONS=([6-9]|[1-9][0-9])/
  EVIDENCE: M46_SECTIONS=8

- [x] G19b: The section number does not collide — exactly one `## M46` exists, and all seven subsections are present
  CHECK: grep -c "^## M46 " docs/DECISIONS-MERGE.md | awk '{print "M46_HEADINGS="$1}' && for n in 1 2 3 4 5 6 7; do grep -q "^### M46\.$n " docs/DECISIONS-MERGE.md || echo "MISSING M46.$n"; done && echo SUBSECTIONS_ALL_PRESENT
  EXPECT: /M46_HEADINGS=1[\s\S]*SUBSECTIONS_ALL_PRESENT/
  EVIDENCE: M46_HEADINGS=1 | SUBSECTIONS_ALL_PRESENT

- [x] G20: baskfynavrefactorreport.md no longer lists full ⌘K search as deferred
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -n "Global ⌘K search" baskfynavrefactorreport.md | head -1
  EXPECT: /Shipped 25 Aug 2026/
  EVIDENCE: 186:5. ~~**Global ⌘K search** across stocks/indices/baskets/screens (F11).~~ **Shipped 25 Aug 2026**

- [x] G21: The status page records the change, loud about what is NOT done
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "Not done, and deliberately so" docs/00-merge-status.md | awk '{print "STATUS_HONEST="$1}'
  EXPECT: /STATUS_HONEST=[1-9]/
  EVIDENCE: STATUS_HONEST=1

- [x] G22: No non-negotiable was touched — still no execute route, desk surfaces still read-only
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test_m40 BASKFY_REDIS_URL=redis://localhost:6380/2 && uv run pytest services/api/tests/test_desk_readonly.py services/api/tests/test_ac_no_orders.py -p no:randomly --tb=line 2>&1 | grep -oE "[0-9]+ passed|[0-9]+ failed" | tr '\n' ' '
  EXPECT: /passed/
  EVIDENCE: 11 passed

**G3 was refuted and rewritten.** Its first CHECK was
`... | tail -2 | tr -d '\n' | grep -qv "F\|E" && echo SEARCH_SUITE_GREEN`, and running it with
`BASKFY_TEST_DATABASE_URL` unset printed `SEARCH_SUITE_GREEN` over **17 skipped tests** — the word
"skipped" carries no `F` or `E`. A gate that passes when nothing ran is worse than no gate. It now
asserts the passed count and nothing else, so it fails on a skip, on a failure, and on a test being
deleted.

ABANDON: G13-whole-app-eslint `pnpm run lint` runs tsc + eslint + a route-shadow check. tsc is
green (G13) and the route-shadow check passes, but eslint reports 25 errors across 12 files this
sitting never opened — `lib/basket/materialize.ts`, `lib/marketing/sample-screen.ts`,
`lib/screens/column-display.ts`, `components/data/data-table.tsx` and others — all owned by trees
running concurrently in this same working tree. Scoped to G13 + G13b, which are the halves this
sitting can honestly stand behind. Declaring the whole command green would have required either
editing other trees' files mid-flight or a false pass; the first draft of this gate ended
`| tail -5 && echo "LINT_EXIT=$?"`, which reported the exit code of `tail` and marked itself met
while the command had failed. That is exactly the failure this file exists to prevent, and it is
recorded rather than quietly corrected.

<!--
A checked box whose EVIDENCE still reads "pending" is UNMET.
Run: node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/tree3-catalog-search.md
-->
