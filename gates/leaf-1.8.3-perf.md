# Gates: leaf-1.8.3-perf

Scope: Catalog list avoids N+1; budget documented

- [ ] G1: perf note or test
  CHECK: rg -n 'N\+1|p95|budget' decile-blueprint/services/api/src/baskfy_api/routers/explore.py decile-blueprint/services/api/tests/test_explore_catalog.py 2>/dev/null | head -1
  EXPECT: /./
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
