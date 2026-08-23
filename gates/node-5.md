# Gates: node-5

Scope: RSC list pages under ~1–2s budget (timeout-capped + structural fixes)

- [x] G1: shared timed fetch exists (≤3000ms)
  CHECK: rg -n 'SERVER_FETCH_TIMEOUT_MS|AbortSignal\.timeout' decile-blueprint/apps/web/src/lib/api -g '*.ts' | head -5
  EXPECT: /
  EVIDENCE: `server-fetch.ts:14 SERVER_FETCH_TIMEOUT_MS` default 2500; `AbortSignal.timeout` in `serverFetchJson` + `timedFetch`. Vitest: `SERVER_FETCH_TIMEOUT_MS ≤ 3000` pass. Hung-fetch microbench: `hung_abort_ms=82 name=TimeoutError`.

- [x] G2: explore/investments/basket/watchlist use timed fetch
  CHECK: rg -n 'serverFetchJson|timedFetch|SERVER_FETCH_TIMEOUT' decile-blueprint/apps/web/src/lib/explore/fetch.ts decile-blueprint/apps/web/src/lib/investments/fetch.ts decile-blueprint/apps/web/src/lib/basket/fetch.ts | head -10
  EXPECT: /
  EVIDENCE: explore `serverFetchJson`; investments/watchlist `serverFetchJsonOrNull`; basket `serverFetchJson` + `BASKFY_BASKET_FETCH_TIMEOUT_MS` default 4000; layout `fetchMe` also timed. Warm curl 2026-08-23: `/explore 0.12s`, `/investments 0.12s`, `/watchlist 0.09s`, `/baskets 0.23s`.

- [x] G3: portfolios client path has timeout
  CHECK: rg -n 'timeout|AbortSignal|SERVER_FETCH|timedFetch' decile-blueprint/apps/web/src/lib/api/server.ts decile-blueprint/apps/web/src/app/\(app\)/portfolios/page.tsx | head -10
  EXPECT: /
  EVIDENCE: `server.ts` wires `timedFetch()` into `serverApi`/`publicApi`; portfolios page documents Tree-5 timed path. Unauth `/portfolios` → 307 login in 0.02s.

- [x] G4: no execute/order paths added
  CHECK: rg -n 'place_order|OrderGateway' decile-blueprint/apps/web/src/lib/explore/fetch.ts decile-blueprint/apps/web/src/lib/investments/fetch.ts decile-blueprint/apps/web/src/lib/basket/fetch.ts decile-blueprint/apps/web/src/lib/api/server-fetch.ts 2>/dev/null; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: exit:1 (no matches). API baskets live-build capped `asyncio.wait_for(..., timeout=3.0)`.

<!-- integrity: security, performance, memory, accuracy required -->
