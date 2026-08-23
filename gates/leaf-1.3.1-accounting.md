# Gates: leaf-1.3.1-accounting

Scope: XIRR, fees 04§1, ledgers

- [x] G1: fee boundary 6666/7000
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=line -k fee 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: 11 passed [100%] 2026-08-23T00:15Z; base(6666)=99.99 total=117.99; base(7000)=100 total=118.00; SIP/zero-fee kinds; ROUND_HALF_UP via baskfy_core.gst.money

- [x] G2: full accounting suite (XIRR + ledgers + realized PnL)
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=line 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: 21 passed [100%] 2026-08-23T00:15Z; XIRR hand fixture 0.1500 and irregular 0.0826 to 4 dp; xirr_displayable >365d; partial-exit loss -800

<!-- integrity: security, performance, memory, accuracy required -->
