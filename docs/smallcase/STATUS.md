# SC run — live status

The status page for the smallcase-layer run. Updated at the end of every module, loud
about what is NOT done. A fresh session resumes from the first module not marked ✅.

**Run state: IN PROGRESS.** SC0–SC2 are green on the isolated `codex/sc-run` worktree.
Resume at SC3. Every command that could reach execution is run with `DRY_RUN=true`.

## Module ledger

| Module | State | One line |
|---|---|---|
| SC0 — Baseline and read-in | ✅ | Clean, live-database baseline recorded; concurrent M41 work isolated without loss. |
| SC1 — Schema and domain objects | ✅ | 18-table exact schema, append-only versions, sole identities, repository, and idempotent reference seeds. |
| SC2 — Catalog computation and API | ✅ | Exact stored metrics, bounded catalog/watchlist API, causal EOD job, and first audited SCAN projection. |
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
  or SIP-reminder jobs existed at the SC0 baseline; SC2 added causal post-publish metrics dispatch
  plus the 20:20 recovery entry. Drift and SIP-reminder jobs still do not exist.

## SC1 evidence — schema and domain objects

- Migration `0014_curated_baskets` adds exactly the 18 documented tables:
  `cb_manager`, `cb_basket`, `cb_basket_version`, `cb_constituent`, `cb_metrics`,
  `cb_collection`, `cb_watchlist_item`, `cb_investment`, `cb_investment_holding`,
  `cb_order_batch`, `cb_fee_ledger`, `cb_dividend`, `cb_sip_plan`, `cb_pending_action`,
  `cb_update_post`, `cb_user_rebalance_state`, `cb_plan`, and `cb_subscription`. Foreign-key
  access paths are indexed, all financial columns are PostgreSQL `numeric`, version dates are
  unique per basket, and database triggers reject version/constituent updates and deletes.
- `CuratedBasketRepository.create_version` locks the basket row, rejects malformed identities or
  drafts before writing, requires four-decimal-representable weights totaling exactly `1.0000`,
  enforces strictly increasing effective dates, and derives GENESIS/CHANGED/NO_CHANGE plus
  add/remove counts. Its 10,000-constituent validation gate is linear and bounded below 4 MiB.
- Reference seeding creates one no-password sole operator plus exactly `baskfy-engine` (ENGINE)
  and `maulik` (HUMAN). Reseeding preserves operator-maintained profile/credential fields,
  converges manager copy, and creates zero auth-token, API-key, subscription, or payment rows.
- Parent integration evidence: all three leaf ledgers report 6/6 gates; 46 SC1 cross-layer tests
  pass against `baskfy_sc_test`; migrate → seed → migrate exits zero and reports two managers;
  repository-wide Ruff/format, strict mypy, API-client TypeScript, and web lint pass with zero
  errors; the unchanged desk remains **1,328 passed, 17 skipped, 12 subtests passed** under
  `DRY_RUN=true`.
- The default application suite initially recorded **1,685 passed, 836 skipped, 1 performance
  failure** while unrelated M41 analysis and web-test processes saturated the shared 10-core host.
  The 500-instrument computation was correct but took 39.5s against its unchanged 30s budget.
  Tests and budgets were not weakened: the normal-resource suite excluding that one benchmark
  passed completely (1,685 passed, 836 skipped, 1 deselected), its MomentumScan byte-stability
  test passed separately under the normal ten-thread pool, and the exact performance benchmark
  passed under a contention-safe three-worker Polars cap. Together those green profiles exercise
  every collected test without changing product code or expected results.

## SC2 evidence — catalog computation and API

- Pure `baskfy_core.curated_metrics` owns exact affordability, whole-share floors, point-in-time
  chain-linked base-100 NAV, calendar-window returns/CAGR, annualized sample volatility, young
  basket constituent fallback, and fixed/tercile buckets. It has no I/O or ambient clock. Its 31
  tests include 150 Hypothesis examples, golden version-switch/no-lookahead cases, exact rounding,
  incomplete-history refusal, and an 8,000-day linear-memory check; an independent 50,000-case
  Fraction oracle also agreed with every affordability allocation.
- Authenticated sole-operator routes now serve `/api/v1/catalog`, basket detail, manager,
  collections, and watchlist GET/PUT/DELETE. Every documented query parameter round-trips;
  FEE access remains dark; future versions do not leak; sub-one-year headlines use
  since-inception returns with calendar-month labels. An 83-basket catalog stays at two SQL
  statements including auth, and an 80-item watchlist with daily change stays at three. Responses
  are capped at 500 and disclose total/truncation. Anonymous and non-sole identities are blocked,
  request user ids cannot select ownership, and no SC route imports or exposes execution,
  payment, provider, or network code.
- Watchlist PUT stores the latest four-decimal chain NAV and is a true conflict no-op; unwatch then
  rewatch records a fresh wall-clock timestamp/baseline. `moved_pct` is exact against that baseline.
  Daily change is returned only when the NSE calendar proves the latest two metric snapshots are
  consecutive sessions; otherwise it is honestly null.
- `baskfy.compute.curated_metrics` is causally dispatched after a successful nightly publish and
  also has an idempotent 20:20 weekday Beat recovery. It verifies the trading/published date,
  uses adjusted closes for returns and raw closes for minimum amount, interval-prunes shared
  set-based price reads, computes every basket before the first write, and preserves
  `computed_at` on identical reruns. Hard bounds are 500 published baskets, 10,000 merged SQL
  intervals, and 250,000 streamed price rows. A 200,000-row/50-name probe computed in 0.608 s at
  about 152 MiB process RSS; fixed-50 15-year and pathological-churn tests pin the safe boundary.
- The first product projection is the private, sole-user-owned `baskfy-momentum-scan`, sourced
  from an already selected NSE-cash MomentumScan top-N (maximum 50), with exact equal 10,000-unit
  largest-remainder weights. Its schema-valid Screen/ScreenRun and immutable notes preserve scan
  id/data-version provenance. Exact reruns change no row/timestamp; different provenance or
  content cannot relabel genesis; no order, credential, payment, or subscription row is created.
- Parent integration evidence: all four SC2 leaf ledgers report 6/6; **85 SC2 tests** pass together
  on `baskfy_sc_test`; migrate/reference-seed-twice/migrate remains green; generated OpenAPI and
  TypeScript client are current; API-client tests remain **110/110**; repository Ruff/format,
  strict mypy, API-client/web TypeScript and ESLint pass with zero errors. The unchanged desk is
  **1,328 passed, 17 skipped, 12 subtests passed** under `DRY_RUN=true`.

## Open items / things a future session must know

- **SC3–SC12 are not done.** SC2 computes and serves catalog/watchlist state only. There is still
  no Friday version cutter, rebalance diff/plan generator, investment accounting engine, product
  page, investor UI, SIP reminder, create/customize flow, engagement ranking, Track-B middleware,
  hardening proof, or final release verification.
- The Track-B plan/subscription tables exist only as dormant schema. Their default-off flags,
  middleware, and 404 proofs do **not** exist until SC10. No fee collection or payment call site
  was added. Track C remains absent, and no new web order route exists.
- The source `main` worktree is concurrently occupied by M41. Do not pop the preserved stash or
  develop SC there. Continue in `/Users/maulikdave/Documents/projects/baskfy-sc-run` on
  `codex/sc-run`.
- SC1/SC2 migration, seeds, projection, and metrics were exercised only against disposable
  `baskfy_sc_*` databases. The production-shaped `baskfy` database was not migrated, seeded, or
  recomputed by either module.
- The default Python suite intentionally skips database tests unless
  `BASKFY_TEST_DATABASE_URL` is supplied. SC0 ran the database suite separately and green; keep
  using `postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_sc_test` for this worktree.
- The local source-worktree desk `.env` had `DRY_RUN=false`; SC0 changed only that ignored safety
  bit to `true`. The isolated worktree has no copied secret-bearing `.env`; commands force
  `DRY_RUN=true` and committed examples already default true.
