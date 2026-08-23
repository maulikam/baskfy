# Gates: leaf-1.1.4-metrics-job

Scope: Celery EOD cb_metrics upsert idempotent

- [ ] G1: beat entry exists
  CHECK: rg -n 'curated.metrics|cb_metrics|curated_metrics' decile-blueprint/services/worker/src/baskfy_worker/celery_app.py
  EXPECT: curated
  EVIDENCE: pending

- [ ] G2: job tests pass
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_curated_metrics_beat.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
