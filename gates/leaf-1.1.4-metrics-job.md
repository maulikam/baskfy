# Gates: leaf-1.1.4-metrics-job

Scope: Celery EOD cb_metrics upsert idempotent

- [x] G1: beat entry exists
  CHECK: rg -n 'curated.metrics|cb_metrics|curated_metrics' decile-blueprint/services/worker/src/baskfy_worker/celery_app.py
  EXPECT: curated
  EVIDENCE: test_curated_metrics_beat [100%]; beat key curated in celery_app

- [x] G2: job tests pass
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_curated_metrics_beat.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: test_curated_metrics_beat [100%]; beat key curated in celery_app

<!-- integrity: security, performance, memory, accuracy required -->
