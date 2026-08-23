# Gates: node-1.3

Scope: SC4 accounting correct to the rupee

- [ ] G1: accounting tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
