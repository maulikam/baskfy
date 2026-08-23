# Gates: leaf-1.3.2-drift

Scope: Drift detection + dividend derivation

- [x] G1: drift tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_drift.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: 9 passed [100%] 2026-08-23T00:15Z; detect_drift shortfall→DRIFT; fix_drift synthetic EXIT; rebase_holdings_after_drift + CUSTOMIZE excess markers; mapping contract helpers

<!-- integrity: security, performance, memory, accuracy required -->
