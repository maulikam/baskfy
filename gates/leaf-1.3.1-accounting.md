# Gates: leaf-1.3.1-accounting

Scope: XIRR, fees 04§1, ledgers

- [ ] G1: fee boundary 6666/7000
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=line -k fee 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
