# Gates: leaf-1.3.2-drift

Scope: Drift detection + dividend derivation

- [ ] G1: drift tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_drift.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
