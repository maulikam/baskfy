# Gates: leaf-3.3-holdings

Scope: Holdings sync endpoint returns HoldingRow shape; no place_order

- [x] G1: holdings sync tests
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_broker_holdings_sync.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ...... [100%] (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
