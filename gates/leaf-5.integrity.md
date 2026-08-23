# Gates: leaf-5.integrity

Scope: Perf fixes do not add order routes

- [x] G1: no OrderGateway in changed fetch libs
  CHECK: rg -n 'OrderGateway|place_order|confirm=true' decile-blueprint/apps/web/src/lib/api/server-fetch.ts decile-blueprint/apps/web/src/lib/explore/fetch.ts decile-blueprint/apps/web/src/lib/investments/fetch.ts decile-blueprint/apps/web/src/lib/basket/fetch.ts; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: exit:1. Vitest server-fetch 4/4. No OrderGateway / place_order / confirm=true in timed-fetch surface.

<!-- integrity: security, performance, memory, accuracy required -->
