# Gates: leaf-4.4-e2e

Scope: explore-handoff can run without full stack via page.route mocks

- [x] G1: spec has route mock or test.use
  CHECK: rg -n 'route\(|page\.route|mock' decile-blueprint/apps/web/e2e/explore-handoff.spec.ts | head -3
  EXPECT: /
  EVIDENCE: page.route for /api/v1/explore* (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
