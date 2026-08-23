# Gates: leaf-2.2-create-api

Scope: POST create PRIVATE basket; weights sum to 1; no execute

- [x] G1: create API tests
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_curated_create.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ..... [100%] 2026-08-23T01:02Z (5 tests: OpenAPI `/api/v1/cb/baskets`, no broker tokens, PRIVATE/STOCK/MANUAL/GENESIS + `assert_weights_sum_to_one` + sole-user, weight reject, persist mock); `OrderGateway` absent in router

<!-- integrity: security, performance, memory, accuracy required -->
