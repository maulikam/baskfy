# Gates: leaf-3.4-remnants

Scope: Create form posts API; dividend Beat job exists

- [x] G1: create form calls /cb/baskets
  CHECK: rg -n 'cb/baskets|/api/v1/cb/baskets' decile-blueprint/apps/web/src/components/create decile-blueprint/apps/web/src/lib/create | head -2
  EXPECT: /
  EVIDENCE: lib/create/fetch.ts POST /api/v1/cb/baskets (2026-08-23T01:18Z)

- [x] G2: dividend beat tests
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_curated_dividends_beat.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: .... [100%] (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
