# Gates: leaf-1.1.3-explore-api

Scope: GET explore/list/detail + watchlist CRUD; no execute

- [ ] G1: explore tests pass
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_explore_catalog.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: pending

- [ ] G2: no mutate execute on explore router
  CHECK: rg -n 'execute|place_order' decile-blueprint/services/api/src/baskfy_api/routers/explore.py || echo none
  EXPECT: none
  EVIDENCE: pending

- [x] G3: watchlist requires auth
  CHECK: rg -n 'AuthenticatedDep|require_user' decile-blueprint/services/api/src/baskfy_api/routers/explore.py
  EXPECT: AuthenticatedDep
  EVIDENCE: 397:    principal: AuthenticatedDep, | 441:    principal: AuthenticatedDep,

<!-- integrity: security, performance, memory, accuracy required -->
