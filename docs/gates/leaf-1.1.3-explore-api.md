# Gates: leaf-1.1.3-explore-api

Scope: GET explore/list/detail + watchlist CRUD; no execute

- [x] G1: explore tests pass
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_explore_catalog.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: test_explore_catalog 6 tests [100%]; explore.router mounted; no execute in explore.py

- [x] G2: no mutate execute on explore router
  CHECK: rg -n 'execute|place_order' decile-blueprint/services/api/src/baskfy_api/routers/explore.py || echo none
  EXPECT: none
  EVIDENCE: test_explore_catalog 6 tests [100%]; explore.router mounted; no execute in explore.py

- [x] G3: watchlist requires auth
  CHECK: rg -n 'AuthenticatedDep|require_user' decile-blueprint/services/api/src/baskfy_api/routers/explore.py
  EXPECT: AuthenticatedDep
  EVIDENCE: 397:    principal: AuthenticatedDep, | 441:    principal: AuthenticatedDep,

<!-- integrity: security, performance, memory, accuracy required -->
