# Gates: leaf-5.baskets

Scope: baskets RSC latency capped / fast empty

- [x] G1: timeout or timed helper in path
  CHECK: rg -n 'AbortSignal|SERVER_FETCH_TIMEOUT|serverFetchJson|timeout' decile-blueprint/apps/web/src/lib/basket/fetch.ts 2>/dev/null | head -5
  EXPECT: /
  EVIDENCE: web `serverFetchJson` timeout default 4000ms (`BASKFY_BASKET_FETCH_TIMEOUT_MS`); API `asyncio.wait_for(build_current_basket, timeout=3.0)`. Warm: `/baskets 200 t=0.185–0.226s`.

<!-- integrity: security, performance, memory, accuracy required -->
