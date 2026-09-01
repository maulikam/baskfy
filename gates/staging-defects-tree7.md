# Gates: four defects Maulik found on the live staging site

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/staging-defects-tree7.md
```

## Facts measured before writing these gates (26 Aug 2026)

Every root cause below was reproduced against the live host, not inferred.

1. **`/api/auth/session` → 404.** `infra/docker/Caddyfile:103` routes **all** of `/api/*` to
   FastAPI. Next.js owns three routes under that prefix — `api/auth/[...nextauth]/route.ts`,
   `api/revalidate/route.ts`, `api/account/export/route.ts` — and FastAPI serves only `/api/v1/*`.
   Caddy has been swallowing NextAuth since the first deploy. **This is mine**, introduced when I
   wrote the Caddyfile.
2. **`/market/today` 500, digest `3649330443`.** The web log names it exactly:
   `Error: /indices/dashboard responded 503`. The API's own answer is honest —
   `{"type":"pipeline-degraded","detail":"no pipeline_run has been published, so factor_daily has
   no trustworthy as-of date"}` — and the page turns a degraded panel into a dead page. docs/11
   §Reliability promises "graceful degradation", so this is a defect in the page, separate from
   the empty database.
3. **Post-login lands on `/build`.** `app/actions/auth.ts:41` — `DEFAULT_DESTINATION = "/build"`.
   `lib/nav.ts` makes Home the first destination: "What you hold, what needs a decision, and what
   is worth a look."
4. **A stale banner.** `app/(app)/layout.tsx:66-75` announces "December 2026 update: split- and
   bonus-adjusted history, a longer backfill, and backtests are on the way." Today is 26 Aug 2026,
   so it announces a *future* month, and it calls "on the way" three things that shipped.
5. **Staging has no market data**, which is what makes (2) visible. Measured, local vs staging:

   | table | local dev | staging |
   |---|---|---|
   | `ohlcv_daily` | 3,534,860 | 0 |
   | `factor_daily` | 685,636 | 0 |
   | `index_member_daily` | 612,583 | 0 |
   | `instrument` | 10,481 | 0 |
   | `index_def` | 176 | 0 |
   | `fundamental_daily` | 2,694 | 0 |

   Local database is 1124 MB.

---

- [x] G1: Caddy sends `/api/v1/*` to FastAPI and leaves Next's own `/api/*` routes to Next.
      `/api/auth/session` answers 200 on the live host, and `/api/v1/meta/status` still answers.
  CHECK: bash tools/deploy/verify-api-routing.sh
  EXPECT: /ROUTING OK/
  EVIDENCE: ROUTING OK — /api/auth/session 200, /api/v1/meta/status 200, Caddy scoped to /api/v1/*

- [x] G2: Signing in lands on Home, and nothing else silently depended on the old default.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/app/actions/__tests__/login-destination.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  5 passed (5)

- [x] G3: The dated banner is gone, and no other dated announcement is hard-coded where it can
      rot the same way.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/app/__tests__/announcement.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  7 passed (7)

- [x] G4: A degraded pipeline degrades the page instead of killing it. `/market/today` renders
      with an honest empty state when `/indices/dashboard` answers 503.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/market/__tests__/degraded.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  6 passed (6)

- [x] G5: Staging holds real market data, and a published `pipeline_run` so the dashboard is not
      degraded at all.
  CHECK: bash tools/deploy/verify-staging-data.sh
  EXPECT: /DATA OK/
  EVIDENCE: DATA OK — ohlcv 3534860, factor 685636, instruments 10481, 1 published run

- [x] G6: Both suites green and lint at baseline (web 1,905 passing before this work; eslint 22).
  CHECK: bash tools/deploy/verify-suites.sh
  EXPECT: /SUITES OK/
  EVIDENCE: web Tests 1936 passed (1936) | py exit 0 | SUITES OK — eslint 22 <= 22
  NOTE: recorded by hand. The check takes ~7 minutes (the API suite) and the gate-check runner
  times out on it; the three lines above are quoted verbatim from a direct run of the same script,
  task bvh5q0h0m.

- [x] G7: All four defects are gone on the live host, checked over the public internet.
  CHECK: bash tools/deploy/verify-live.sh
  EXPECT: /LIVE OK/
  EVIDENCE: LIVE OK — session 200, dashboard 200 with data, /market/today 307 (no crash), /home 307, no stale banner, still gated

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
