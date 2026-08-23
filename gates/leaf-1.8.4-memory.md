# Gates: leaf-1.8.4-memory

Scope: Metrics job chunked; no unbounded load

- [x] G1: explicit chunk size in metrics service or worker
  CHECK: rg -n "CHUNK|chunk_size|BATCH_SIZE|for .* in .*\[:" decile-blueprint/services/api/src/baskfy_api/curated_metrics_service.py decile-blueprint/services/worker/src/baskfy_worker/tasks/curated_metrics.py | head -3
  EXPECT: /CHUNK|chunk|BATCH/
  EVIDENCE: 2026-08-23 — `METRICS_BASKET_CHUNK = 50` in `curated_metrics_service.py`;
  `compute_all_metrics` loads basket ids then iterates ORM rows in `chunk_size` batches
  with flush between chunks. Asserted by `test_explore_perf.py::test_metrics_basket_chunk_is_bounded`.

<!-- integrity: security, performance, memory, accuracy required -->
