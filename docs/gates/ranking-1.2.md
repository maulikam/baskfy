# Gates: ranking-1.2 — Explainability UI + rank history

Scope: When `sort_by=desk_score`, the web UI surfaces the book's A–F breakdown.
Rank history uses the existing `GET /instruments/{symbol}/rank-history` + `screen_run`
audit trail when a saved `screenPublicId` is available.

- [x] G1: Desk explain column display labels exist for A–F keys
  CHECK: rg -n "desk_a_trend" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/lib/screens/column-display.ts
  EXPECT: desk_a_trend
  EVIDENCE: 53:  desk_a_trend: { | 92:  "desk_a_trend",

- [x] G2: Desk explain columns are excluded from the main results table diet
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/column-display.test.ts -t "desk" 2>&1 | tail -8
  EXPECT: /passed|Tests.*\d+ passed/
  EVIDENCE: Start at  19:20:33 | Duration  1.08s (transform 60ms, setup 132ms, collect 78ms, tests 1ms, environment 469ms, prepare 51ms)

- [x] G3: Peek drawer renders a desk A–F breakdown when explain columns are present
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/screens/__tests__/peek-drawer.test.tsx -t "desk" 2>&1 | tail -10
  EXPECT: /passed|Tests.*\d+ passed/
  EVIDENCE: Start at  19:20:09 | Duration  894ms (transform 75ms, setup 63ms, collect 192ms, tests 98ms, environment 270ms, prepare 58ms)

- [x] G4: Rank history hook/query exists against the existing API path
  CHECK: rg -n "rank-history" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/lib/screens/queries.ts /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/
  EXPECT: /rank-history/
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/lib/screens/queries.ts:246:    queryKey: ["instruments", symbol, "rank-history", screenPublicId] as const, | /Users/maulikdave

- [x] G5: Decision records explainability UI + rank-history wiring
  CHECK: rg -n "Explainability UI" /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md
  EXPECT: Explainability UI
  EVIDENCE: 7480:**Not done.** Explainability UI; CSV export for desk_score; research factors; portfolio selection.

- [x] G6: PLAN status log notes Phase 1.2 UI
  CHECK: rg -n "Phase 1.2.*explain|explainability UI" /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: /explain/i
  EVIDENCE: 62:- 2026-09-13 Phase 1.2 UI: explainability UI — peek A–F breakdown; desk columns out of table diet;
