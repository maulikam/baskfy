# Gates: leaf-1.2.3-hours

Scope: Market-hours guard returns closed-market payload

- [x] G1: hours tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_market_hours_cb.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ..............                                                           [100%] (14 passed; closed payload `{market_open: false, next_open_ist}` — never a plan)

<!-- integrity: security, performance, memory, accuracy required -->
