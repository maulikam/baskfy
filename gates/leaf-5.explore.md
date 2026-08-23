# Gates: leaf-5.explore

Scope: explore RSC latency capped / fast empty

- [x] G1: timeout or timed helper in path
  CHECK: rg -n 'AbortSignal|SERVER_FETCH_TIMEOUT|serverFetchJson|timeout' decile-blueprint/apps/web/src/lib/explore/fetch.ts 2>/dev/null | head -5
  EXPECT: /
  EVIDENCE: `serverFetchJson` + `ServerFetchTimeoutError` → `ExploreUnavailable`. Warm: `/explore 200 t=0.117–0.264s` (2026-08-23). Layout Suspense streams shell while page resolves.

<!-- integrity: security, performance, memory, accuracy required -->
