# Gates: node-1.3

Scope: SC4 accounting correct to the rupee

- [x] G1: accounting tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: parent re-run — 21 passed [100%]; fee -k 11 [100%]; 6666→117.99 / 7000→118.00

- [x] G2: drift tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_drift.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: parent re-run — 9 passed [100%]

<!-- integrity: security, performance, memory, accuracy required -->
