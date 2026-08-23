# Gates: leaf-1.2.2-plans

Scope: Invest/apply/exit plan gen → desk plan_id; no gateway execute

- [ ] G1: plan tests; no OrderGateway execute from web router
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_curated_plans.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
