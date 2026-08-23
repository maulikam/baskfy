# Gates: leaf-3.2-oauth

Scope: Callback route + encrypted state/token helpers

- [x] G1: callback route in OpenAPI
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; print('ok' if any('callback' in p for p in create_app().openapi()['paths']) else 'missing')"
  EXPECT: ok
  EVIDENCE: ok (2026-08-23T01:18Z)

- [x] G2: oauth helper tests
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_broker_oauth.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ....... [100%] (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
