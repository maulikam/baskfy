# Gates: leaf-5.portfolios

Scope: portfolios RSC latency capped / fast empty

- [x] G1: timeout or timed helper in path
  CHECK: rg -n 'timeout|AbortSignal|SERVER_FETCH|timedFetch' decile-blueprint/apps/web/src/lib/api/server.ts decile-blueprint/apps/web/src/app/\(app\)/portfolios/page.tsx 2>/dev/null | head -5
  EXPECT: /
  EVIDENCE: `serverApi()` → `timedFetch()` (default 2500ms). Page comment documents Tree-5. Unauth gate: 307 `/login?next=/portfolios` in ~0.02s.

<!-- integrity: security, performance, memory, accuracy required -->
