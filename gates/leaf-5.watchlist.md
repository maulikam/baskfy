# Gates: leaf-5.watchlist

Scope: watchlist RSC latency capped / fast empty

- [x] G1: timeout or timed helper in path
  CHECK: rg -n 'AbortSignal|SERVER_FETCH_TIMEOUT|serverFetchJson|timeout' decile-blueprint/apps/web/src/lib/investments/fetch.ts 2>/dev/null | head -5
  EXPECT: /
  EVIDENCE: `fetchWatchlist` → `serverFetchJsonOrNull` → `{items:[],count:0}` on timeout/401. Warm: `/watchlist 200 t=0.093–0.149s`.

<!-- integrity: security, performance, memory, accuracy required -->
