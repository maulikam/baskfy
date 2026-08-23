# Gates: leaf-1.8.5-accuracy

Scope: Fee and XIRR fixtures match 04

- [ ] G1: fee fixture test
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
