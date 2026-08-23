# Gates: leaf-1.1.2-scan

Scope: Deterministic MomentumScan → genesis version projection

- [x] G1: scan_projection tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_scan_projection.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: scan_projection tests [100%] 23 Aug

- [x] G2: weights sum to 1 in projection
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_scan_projection.py -q --tb=line -k weight
  EXPECT: [100%]
  EVIDENCE: scan_projection tests [100%] 23 Aug

<!-- integrity: security, performance, memory, accuracy required -->
