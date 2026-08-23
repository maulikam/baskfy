# Gates: leaf-1.8.5-accuracy

Scope: Fee and XIRR fixtures match 04

- [x] G1: fee fixture test
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: 2026-08-23 — `..................... [100%]` (21 passed). Fee straddle
  ₹6666/₹7000 (117.99 vs 118.00). XIRR hand fixtures: one-year 0.1500; irregular
  three-flow 0.0826 (4 dp). Realized PnL partial-exit loss −800.00. Pure Decimal;
  half-up 2 dp via `baskfy_core.gst.money`.

<!-- integrity: security, performance, memory, accuracy required -->
