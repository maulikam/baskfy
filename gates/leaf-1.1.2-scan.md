# Gates: leaf-1.1.2-scan

Scope: Deterministic MomentumScan → genesis version projection

- [ ] G1: scan_projection tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_scan_projection.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: pending

- [ ] G2: weights sum to 1 in projection
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_scan_projection.py -q --tb=line -k weight
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
