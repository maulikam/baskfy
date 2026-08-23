# Gates: RSC page performance (tree 6 / node-5)

Scope: Cut /investments, /portfolios, /explore, /watchlist, /baskets RSC latency from multi-second hangs.

- [x] G1: explore page fetch path has timeout/budget or parallelization
  CHECK: rg -n 'timeout|AbortSignal|Promise\.all|cache|revalidate|force-dynamic' decile-blueprint/apps/web/src/app/\(app\)/explore decile-blueprint/apps/web/src/lib/explore -g '*.{ts,tsx}' | head -15
  EXPECT: /
  EVIDENCE: `lib/explore/fetch.ts` → `serverFetchJson` (AbortSignal.timeout 2500). Layout `Promise.all([auth,cookies,headers,fetchMe])` + Suspense children. Warm `/explore` ~0.12s.

- [x] G2: investments page does not block on missing API forever
  CHECK: rg -n 'timeout|AbortSignal|Promise\.all|fetch' decile-blueprint/apps/web/src/app/\(app\)/investments decile-blueprint/apps/web/src/lib/investments -g '*.{ts,tsx}' | head -15
  EXPECT: /
  EVIDENCE: `serverFetchJsonOrNull` empty fallback. Warm `/investments` ~0.12–0.52s.

- [x] G3: portfolios list fetch has timeout or cache
  CHECK: rg -n 'timeout|AbortSignal|timedFetch|SERVER_FETCH' decile-blueprint/apps/web/src/app/\(app\)/portfolios decile-blueprint/apps/web/src/lib/api/server.ts -g '*.{ts,tsx}' | head -15
  EXPECT: /
  EVIDENCE: `timedFetch` on openapi-fetch client. Unauth 307 in ~20ms.

- [x] G4: watchlist + baskets same pattern
  CHECK: rg -n 'timeout|AbortSignal|serverFetchJson' decile-blueprint/apps/web/src/lib/basket/fetch.ts decile-blueprint/apps/web/src/lib/investments/fetch.ts 2>/dev/null | head -20
  EXPECT: /
  EVIDENCE: both use timed helpers; baskets API wait_for 3s. Warm watchlist/baskets <0.25s.

- [x] G5: shared API fetch helper enforces default timeout ≤3s (or documented budget)
  CHECK: rg -n 'TIMEOUT|timeoutMs|AbortSignal\.timeout|signal:' decile-blueprint/apps/web/src/lib -g '*.{ts,tsx}' | head -20
  EXPECT: /
  EVIDENCE: `SERVER_FETCH_TIMEOUT_MS=2500`; vitest asserts ≤3000; `hung_abort_ms=82 TimeoutError`.

- [x] G6: no place_order / execute added
  CHECK: rg -n 'place_order|OrderGateway|/execute' decile-blueprint/apps/web/src/app/\(app\)/explore decile-blueprint/apps/web/src/app/\(app\)/investments decile-blueprint/apps/web/src/lib/explore decile-blueprint/apps/web/src/lib/investments 2>/dev/null; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: exit:1.

<!-- integrity: security, performance, memory, accuracy required -->
