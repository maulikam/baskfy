# Gates: leaf-2.5-e2e

Scope: Playwright explore → invest handoff smoke

- [x] G1: e2e spec exists and is listed
  CHECK: test -f decile-blueprint/apps/web/e2e/explore-handoff.spec.ts && echo yes
  EXPECT: yes
  EVIDENCE: yes — `decile-blueprint/apps/web/e2e/explore-handoff.spec.ts` (Invest → PlanHandoff; skips without baseURL/auth/catalog; never clicks execute)

<!-- integrity: security, performance, memory, accuracy required -->
