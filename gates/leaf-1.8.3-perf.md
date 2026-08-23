# Gates: leaf-1.8.3-perf

Scope: Catalog list avoids N+1; budget documented

- [x] G1: perf note or test
  CHECK: rg -n 'N\+1|p95|budget' decile-blueprint/services/api/src/baskfy_api/routers/explore.py decile-blueprint/services/api/tests/test_explore_catalog.py 2>/dev/null | head -1
  EXPECT: /./
  EVIDENCE: 2026-08-23 — explore list docstring + inline comment document N+1 avoided and
  catalog p95 < 1s. `test_explore_perf.py` (2) + catalog budget assert (1) = 3 passed [100%].

<!-- integrity: security, performance, memory, accuracy required -->
