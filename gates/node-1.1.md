# Gates: node-1.1

Scope: SC2 branch integrated: metrics+API+job+seed

- [x] G1: curated_metrics tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q --tb=no
  EXPECT: [100%]
  EVIDENCE: SC2 commit 5673e10; metrics+explore+beat green

- [x] G2: explore router mounted
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; print('ok' if any('/explore' in p for p in create_app().openapi()['paths']) else 'missing')"
  EXPECT: ok
  EVIDENCE: ok

<!-- integrity: security, performance, memory, accuracy required -->
