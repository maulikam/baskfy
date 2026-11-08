# Gates: leaf-5.investments

Scope: investments RSC latency capped / fast empty

- [x] G1: timeout or timed helper in path
  CHECK: rg -n 'AbortSignal|SERVER_FETCH_TIMEOUT|serverFetchJson|timeout' decile-blueprint/apps/web/src/lib/investments/fetch.ts 2>/dev/null | head -5
  EXPECT: /
  EVIDENCE: `serverFetchJsonOrNull` → empty list on timeout/404. Warm: `/investments 200 t=0.122–0.516s`. API `/cb/investments` 404 fails fast into empty UI.

<!-- integrity: security, performance, memory, accuracy required -->
