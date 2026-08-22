# SC run — live status

The status page for the smallcase-layer run. Updated at the end of every module, loud
about what is NOT done. A fresh session resumes from the first module not marked ✅.

**Run state: IN PROGRESS.** SC0 is green on the isolated `codex/sc-run` worktree. Resume at
SC1. Every command that could reach execution is run with `DRY_RUN=true`.

## Module ledger

| Module | State | One line |
|---|---|---|
| SC0 — Baseline and read-in | ✅ | Clean, live-database baseline recorded; concurrent M41 work isolated without loss. |
| SC1 — Schema and domain objects | ⬜ | |
| SC2 — Catalog computation and API | ⬜ | |
| SC3 — Versions, rebalance engine, plans | ⬜ | |
| SC4 — Investment accounting | ⬜ | |
| SC5 — Web UI: discovery and detail | ⬜ | |
| SC6 — Web UI: investor surfaces | ⬜ | |
| SC7 — SIP reminders | ⬜ | |
| SC8 — Create and customize | ⬜ | |
| SC9 — Engagement | ⬜ | |
| SC10 — Gating and Track-B dark machinery | ⬜ | |
| SC11 — Hardening and safety proof | ⬜ | |
| SC12 — Verification and final report | ⬜ | |

States: ⬜ not started · 🔄 in progress (with enough written state to resume losslessly) ·
✅ green (committed) · ⛔ blocked (say by what, and what proceeded instead).

## Baselines (SC0 fills this)

- Repository/branch state at run start: `main` at
  `aed71edb9b52e8dcb5ec8ce8206e063bad9e571f` (`M40: green — the palette is the broker
  screen's`). The source worktree already contained an uncommitted M41 broker/backtest change
  and this untracked SC commissioning pack. Another Claude process was actively writing M41, so
  SC runs in `/Users/maulikdave/Documents/projects/baskfy-sc-run` on `codex/sc-run`, forked from
  that SHA. The first observed M41 snapshot is recoverable as `stash@{0}` / object
  `dda9c25c0f15431905a6a3c94dcc5806e6c31ef7`; the live M41 worktree was left untouched after
  isolation. See DECISIONS-SC SC0.1.
- Test suite pass counts at the SC branch baseline:
  - desk: **1,328 passed, 17 skipped, 12 subtests passed** in 44.02s, forced
    `DRY_RUN=true`;
  - screener/API Python default suite: **1,614 passed, 812 skipped** in 116.91s;
  - database-marked Python suite, against the disposable and SC-only `baskfy_sc_test`
    database: **809 passed, 1 skipped, 1,616 deselected** in 664.12s;
  - generated API client: **110 passed**; web unit/component suite: **671 passed** across
    29 files.
- Relevant tables actually found in the running PostgreSQL 16/TimescaleDB instance:
  - instruments/prices: `public.instrument`, `public.ohlcv_daily`;
  - current generated basket: `public.basket_snapshot`;
  - tracker holdings: `public.portfolio`, `public.portfolio_holding`;
  - desk plans/journal-shaped records: `desk.rebalance_versions`,
    `desk.rebalance_orders`, `desk.fills`, `desk.trades` (there is no table literally named
    `journal`);
  - desk holdings history: `desk.snapshots.holdings_json` (there is no desk table literally
    named `holdings`);
  - corporate actions available to SC4: `public.corporate_action` and
    `desk.corporate_actions`.
- Existing `/baskets` pages: `decile-blueprint/apps/web/src/app/(app)/baskets/page.tsx` and
  `.../baskets/plan/page.tsx`. Their server reads are in `apps/web/src/lib/basket/fetch.ts`.
  `GET /api/v1/baskets` serves the latest `public.basket_snapshot`, falling back to the live
  bars → factors → score → basket engine; `GET /api/v1/baskets/plan` reads the most recent
  `desk.rebalance_versions` and its `desk.rebalance_orders`. Both are deliberately read-only.
- Celery Beat inventory (all `Asia/Kolkata`): desk daily collection 18:30 Mon–Fri; desk
  autorun safety net 18:50 Mon–Fri; nightly pipeline 18:45 Mon–Fri; account purge 03:00 daily;
  integrity audit 02:00 Saturday; abandoned-run reaper every 15 minutes; publish SLO check
  20:15 Mon–Fri; Kite-token check hourly at minute 5; queue-backlog check every 10 minutes;
  screen-alert dispatch 20:30 Mon–Fri; webhook sweep every 2 minutes. No SC metrics, drift,
  or SIP-reminder jobs exist yet.

## Open items / things a future session must know

- **SC1–SC12 are not done.** No `cb_` table exists at this point and no SC API or page exists.
- The source `main` worktree is concurrently occupied by M41. Do not pop the preserved stash or
  develop SC there. Continue in `/Users/maulikdave/Documents/projects/baskfy-sc-run` on
  `codex/sc-run`.
- The live database has the merge-era schema only. SC migrations must be exercised first against
  `baskfy_sc_test`; nothing in SC0 mutated the production-shaped `baskfy` database.
- The default Python suite intentionally skips database tests unless
  `BASKFY_TEST_DATABASE_URL` is supplied. SC0 ran the database suite separately and green; keep
  using `postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test` for this worktree.
- The local source-worktree desk `.env` had `DRY_RUN=false`; SC0 changed only that ignored safety
  bit to `true`. The isolated worktree has no copied secret-bearing `.env`; commands force
  `DRY_RUN=true` and committed examples already default true.
