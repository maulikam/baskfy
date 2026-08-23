# Gates: leaf-1.8.1-sec-orders

Scope: Extended no-order-route suite covers explore+cb

- [x] G1: forbidden mutating paths absent
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_baskets_readonly.py services/api/tests/test_explore_catalog.py -q --tb=no -k 'order or explore or readonly' 2>&1 | tail -3
  EXPECT: [100%]
  EVIDENCE: test_explore_no_orders.py 2 passed; OpenAPI mutating watchlist only POST/DELETE watchlist — no execute

<!-- integrity: security, performance, memory, accuracy required -->
