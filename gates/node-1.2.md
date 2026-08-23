# Gates: node-1.2

Scope: SC3 versions+plans integrated without web execute

- [x] G1: version/plan/hours modules exist and tests green
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_versions.py packages/core/tests/test_market_hours_cb.py packages/core/tests/test_curated_plans_core.py services/api/tests/test_curated_plans.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: parent re-run 2026-08-23 — versions 24 + hours 14 + plans core/api 10 = [100%]

- [x] G2: curated_plans router has no OrderGateway
  CHECK: rg -n "OrderGateway|place_order|confirm=true" decile-blueprint/services/api/src/baskfy_api/routers/curated_plans.py; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: no_exec_ok (rg no matches)

<!-- integrity: security, performance, memory, accuracy required -->
