# Gates: leaf-1.8.4-memory

Scope: Metrics job chunked; no unbounded load

- [x] G1: chunk/batch in metrics service
  CHECK: rg -n 'chunk|batch|limit|yield' decile-blueprint/services/api/src/baskfy_api/curated_metrics_service.py decile-blueprint/services/worker/src/baskfy_worker/tasks/curated_metrics.py | head -1
  EXPECT: /./
  EVIDENCE: decile-blueprint/services/api/src/baskfy_api/curated_metrics_service.py:109:            .limit(1)

<!-- integrity: security, performance, memory, accuracy required -->
