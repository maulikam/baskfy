# SC run — live status

The status page for the smallcase-layer run. Updated at the end of every module, loud
about what is NOT done. A fresh session resumes from the first module not marked ✅.

**Run state: SC12 green, then independently audited and found not green.** Started 23 Aug 2026.
See `SC-FINAL-REPORT.md`, `SC-AC-REPORT.md`, and the section
"Independent verification" below, which is the part those two reports could not contain.

## Module ledger

| Module | State | One line |
|---|---|---|
| SC0 — Baseline and read-in | ✅ | Baselines recorded; desk 1328 passed; DRY_RUN=true; M41 catalog uncommitted at start |
| SC1 — Schema and domain objects | ✅ | 18 `cb_*` tables + migration 0014; domain asserts; manager seed; tests green |
| SC2 — Catalog computation and API | ✅ | Pure metrics + `/explore` API + Beat `cb-eod-metrics` + SCAN seed; 26 tests green |
| SC3 — Versions, rebalance engine, plans | ✅ | Diff/versions + market hours + plan preview API (`cb-sim-*`); no OrderGateway |
| SC4 — Investment accounting | ✅ | Fees 6666/7000, XIRR 4dp, ledgers, drift rebase; pure core |
| SC5 — Web UI: discovery and detail | ✅ | `/explore`, `/basket/[slug]`, PlanHandoff stubs; `/baskets` soft-coexist |
| SC6 — Web UI: investor surfaces | ✅ | `/investments*`, `/watchlist`, `/fees`; handoff CTAs; read-only tests |
| SC7 — SIP reminders | ✅ | Pure `curated_sip`: REMINDER-only, holiday next_fire, fire key, SIP_DUE dict; 19 tests |
| SC8 — Create and customize | ✅ | `/create` PRIVATE form; ≥2 instruments; weights normalize to 1.0; preview stubbed |
| SC9 — Engagement | ✅ | `/cb/pending-actions` + `/cb/updates` + dismiss/resolve; `PendingActionCard`; tests green |
| SC10 — Gating and Track-B dark machinery | ✅ | Track B flags default false; paywall/signup/fee-collect 404; Free Access helper |
| SC11 — Hardening and safety proof | ✅ | No-order, tenant, N+1/p95, METRICS_BASKET_CHUNK=50, fee/XIRR accuracy |
| SC12 — Verification and final report | ✅ | `SC-FINAL-REPORT.md`; 119 core + 39 API curated collected; Track C held |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked.

## Baselines (SC0)

Recorded 23 Aug 2026 on macOS (darwin 24), repo root `/Users/maulikdave/Documents/projects/baskfy`.

### Repository state

| | |
|---|---|
| HEAD | `aed71ed` on `main` |
| Working tree at SC0 start | Dirty: uncommitted **M41** broker-catalog work + `docs/smallcase/` pack (this folder). M41 committed as part of clearing the deck before SC1 |
| `DRY_RUN` | **true** in `kite-momentum-rebalancer/.env`, root `.env.example`, desk `.env.example` |

### Test suites at baseline

| Suite | Result |
|---|---|
| Desk (`kite-momentum-rebalancer`, `DRY_RUN=true pytest`) | **1328 passed, 17 skipped, 0 failed** (56.9s) |
| M41 broker tests (core + execution + api) | **13 passed** |
| Web nav (vitest, post-M41 `/brokers`) | **10 passed** |
| Full screener `uv run pytest` | Not re-run end-to-end this session (~80s historically). Last merge-status baseline was 1385 passed / 794 skipped. **SC1 will not land unless `make test` / targeted suites stay green.** |

### Screener Postgres tables (SQLAlchemy `Base.metadata`, 61)

Relevant to this run:

- **Instruments / prices:** `instrument`, `ohlcv_daily`, `corporate_action`, `trading_day`, `factor_daily`, `index_*`, `market_health_daily`
- **Portfolios (CSV path):** `portfolio`, `portfolio_holding`, `portfolio_rebalance`, `portfolio_sleeve`
- **Baskets (M22/M30):** `basket_snapshot`, `screen_run`, `screen`
- **Accounts / curated baskets:** `app_user`, `subscription`, `plan`, …; **`cb_*` (18 tables, SC1)**

### Desk schema (M19 cutover, read by `/performance` etc.)

Tables the web desk routers query today: `desk.snapshots`, `desk.benchmark`, `desk.trades`,
`desk.regime_evaluations`, `desk.regime_exposure`, `desk.rebalance_versions`,
`desk.rebalance_orders`. (M0 also recorded `fills` / holdings-shaped data in the SQLite era —
Postgres desk schema is what SC4 drift compares against.)

### Existing `/baskets` inventory (M22+)

| Route | Source |
|---|---|
| `/baskets` | `baskfy_api.baskets` → nightly `basket_snapshot` (M30) or live build |
| `/baskets/plan` | Same package; plan vs book, **read-only** (no execute) |
| API | `GET /api/v1/baskets`, `GET /api/v1/baskets/plan` |
| Lib | `apps/web/src/lib/basket/fetch.ts` |

SC5 must merge-or-redirect these with the curated-basket catalog (`DECISIONS-SC` at SC5).

### Also present (M41, not SC)

`/brokers` — ten-broker connect grid; live OAuth gated on `BROKER_OAUTH_REVIEW` / D3.

### Celery Beat inventory (`baskfy_worker.celery_app.BEAT_SCHEDULE`)

| Key | Task | When (IST) |
|---|---|---|
| desk-daily-collection | `baskfy.desk.daily` | Mon–Fri 18:30 |
| desk-autorun-safety-net | `baskfy.desk.autorun` | Mon–Fri 18:50 |
| refresh-reference-data | `baskfy.pipeline.nightly` | Mon–Fri 18:45 |
| purge-deleted-accounts | `baskfy.accounts.purge` | Daily 03:00 |
| weekly-integrity-audit | `baskfy.pipeline.integrity_audit` | Sat 02:00 |
| reap-abandoned-pipeline-runs | `baskfy.ops.reap_abandoned_runs` | */15 min |
| publish-deadline-slo | `baskfy.ops.check_publish_deadline` | Mon–Fri 20:15 |
| kite-token-expiry | `baskfy.ops.check_kite_token` | :05 hourly |
| queue-backlog | `baskfy.ops.check_queue_backlog` | */10 min |
| dispatch-screen-alerts | `baskfy.alerts.dispatch` | Mon–Fri 20:30 |
| sweep-webhook-deliveries | `baskfy.alerts.sweep_webhooks` | */2 min |
| cb-eod-metrics | `baskfy.cb.compute_metrics` | Mon–Fri 20:20 |

## SC1 deliverables (23 Aug 2026)

**Built**

- SQLAlchemy models for all 18 `cb_*` tables in `baskfy_core.models.curated_baskets` (Track B included)
- Alembic migration `0014_curated_baskets` revising `0013_portfolio_sleeves`
- Pure domain in `baskfy_core.curated_baskets` (`assert_weights_sum_to_one`, version immutability, sole-user env constant, manager slug constants)
- Seed helpers in `baskfy_api.curated_seed` wired into `seed_reference` (`make seed` upserts `baskfy-engine` + `maulik`)
- Tests: `packages/core/tests/test_curated_baskets.py`, `services/api/tests/test_curated_schema.py`; `test_schema_matches_docs` updated for `cb_*`

**NOT done (by design — later modules)**

- No catalog API, metrics job, or web UI (SC2–SC5)
- No basket rows seeded beyond managers — baskets/versions/constituents are SC2/SC3
- `/baskets` (M22 `basket_snapshot`) unchanged; mapping to explore catalog deferred to SC5
- ~~Track B flags and unreachable-surface tests (SC10)~~ **done SC10**
- `BASKFY_SOLE_USER_ID` resolver exists but no `cb_*` user rows written until SC4

## SC2 deliverables (23 Aug 2026)

**Built**

- `baskfy_core.curated_metrics` — `min_amount` / `shares_at_amount` (04 §2), PACK.1 volatility
  buckets, absolute return / CAGR / chain-link NAV / headline picker
- `baskfy_core.scan_projection` — deterministic MomentumScan top-N → genesis version
- Catalog API at `/api/v1/explore` (+ managers, collections) and `/api/v1/watchlist` CRUD;
  **no order/execute routes**
- Celery task `baskfy.cb.compute_metrics` + Beat `cb-eod-metrics` (Mon–Fri 20:20 IST) upserting
  `cb_metrics` idempotently
- First SCAN basket seed (`momentum-scan`) via `curated_seed.seed_momentum_scan_basket`
- Tests: core property + golden NAV; API filter params (OpenAPI + handler); metrics re-run noop;
  SCAN seed idempotent; Beat registration. DB tests skip without `BASKFY_TEST_DATABASE_URL`.

**NOT done (by design — later modules)**

- Web `/explore` UI (SC5); version publishing / plans (SC3); investor ledgers / XIRR (SC4)
- Full multi-version chain-linked history over real 2011→now bars (job computes from latest
  version weights + available closes; thin history leaves return windows null)
- Collections content seed (SC9); watchlist `moved_pct` needs live NAV (SC4)
- ~~Legacy `/baskets` merge-or-redirect (SC5)~~ soft-coexist recorded in DECISIONS-SC SC5
- Unlazy / gates tree if present beside this run is out of band for the SC2 commit message

## SC5 deliverables (23 Aug 2026) — in progress / leaf green

**Built**

- `/explore` catalog page (URL filter chips → `GET /api/v1/explore`)
- `/basket/[slug]` overview + `/basket/[slug]/constituents` stub
- Shared components: BasketCard, VolatilityChip, AccessBadge, ReturnStat, DisclosureBlock
- `PlanHandoffPanel` + `MarketClosedModal` stubs; Invest CTA never posts an order
- Nav Explore entry; soft `/baskets` notice linking to Explore
- DECISIONS-SC SC5 ⚠ UNREVIEWED; gates leaf-1.4.1 / 1.4.2 / node-1.4 checked

**NOT done (later SC5 polish / SC3+)**

- Performance chart (SIP + benchmark), manager/collections routes, watchlist toggle on cards
- Full constituents timeline (needs SC3 versions)
- E2E Playwright journey for filter → card → detail


## SC7 SIP + SC8 create deliverables (23 Aug 2026) — leaves 1.6.1 / 1.6.2

**Built**

- `baskfy_core.curated_sip` — next_fire holiday snap, pause/resume, fire key, SIP_DUE pending dict;
  AUTO refused on write; no OrderGateway
- `packages/core/tests/test_curated_sip.py` — 19 passed [100%]
- `/create` page + `CreateBasketForm` — PRIVATE framing, ≥2 instruments, equal/custom normalize
- `lib/create/weights.ts` mirrors domain weight sum contract; preview stubbed
- DECISIONS-SC SC7/SC8 ⚠ UNREVIEWED; gates leaf-1.6.1 / 1.6.2 / node-1.6 G1 checked

**NOT done (later)**

- Celery Beat SIP due job + DB persistence of pending actions
- Create API router + catalog PRIVATE visibility E2E
- Customize-flow on existing investments (CUSTOMIZE batches)


## SC9 engage + SC10 Track B (23 Aug 2026) — leaves 1.6.3 / 1.7

**Built**

- `GET/POST /api/v1/cb/pending-actions` (+ dismiss/resolve) and `GET /cb/updates` for the sole user
- `PendingActionCard` on `/investments`; wired in `app.py` via `curated_engage` + `track_b` routers
- Track B settings default false: `subscriptions_enabled`, `fee_collection_enabled`, `public_signup_enabled`
- Dark surfaces `/cb/paywall`, `/cb/public-signup`, `/cb/fees/collect` → 404 while flags off; Free Access helper
- Tests: `test_curated_engage.py` + `test_track_b_gates.py` green; gates leaf-1.6.3 / 1.7 checked

**NOT done (later)**

- Trending jobs / collections seed / unread-dot persistence (broader SC9 AC)
- Flag-on (test-env only) lock UI on a FEE fixture; broker-session guard matrix (rest of SC10)


## SC3 plans + hours deliverables (23 Aug 2026) — leaves 1.2.2 / 1.2.3

**Built**

- `baskfy_core.market_hours_cb` — pure NSE session guard + closed-market payload
- `baskfy_core.curated_plans` — invest / apply / exit desk-shaped builders (30m TTL hint)
- `POST /api/v1/cb/plans/{invest,apply,exit}` — `PLANNED` + synthetic `cb-sim-…` desk_plan_id when open;
  closed-market payload when shut; **no execute / OrderGateway**
- Wired in `app.py` via import + `include_router` only
- Tests: `test_market_hours_cb.py` [100%] · `test_curated_plans.py` [100%]

**NOT done (sibling / later)**

- Full SC3 AC loop (seed → invest → simulated fill → publish v2 → apply) across services
- Persisting `cb_order_batch` / journal sync to EXECUTED
- Web PlanHandoff / MarketClosed UI (leaf 1.5.2) — **done in SC6** (reuse of SC5 stubs +
  InvestmentActions wiring)

## Open items / things a future session must know

1. **D3 still unanswered** (`NEEDS-MAULIK` item 13). Track C stays forbidden: no web execute, no third-party broker OAuth, no payment collection.
2. **M41 was uncommitted when SC0 started**; commit it before SC1 so the working tree only carries SC work.
3. Full screener suite was not re-timed this session — treat desk green + M41/nav green as the SC0 safety bar; expand before SC12.
4. ~~`docs/smallcase/03` `cb_*` schema does not exist in Alembic yet — SC1's job.~~ **Done (0014).**
5. ~~Legacy `/baskets` vs explore catalog collision is deferred to SC5~~ **SC5 soft-coexist:** Explore = public catalog; `/baskets` = desk MomentumScan (banner + CTA, no hard redirect). See DECISIONS-SC SC5.
6. Explore list filter params are documented in `DOCUMENTED_LIST_PARAMS` / DECISIONS-SC SC2 (05-ui-spec chips mapped to query names).


## SC4 core accounting (leaf 1.3.1 / 1.3.2 / 1.8.5) — 23 Aug 2026

**Built (pure core only; no commit this leaf)**

- `baskfy_core.curated_accounting` — platform fees (04 §1), money-put-in / current investment /
  value / returns, realized PnL, ACT/365 XIRR + >365d display gate
- `baskfy_core.curated_drift` — shortfall → DRIFT action shape; fix → synthetic EXIT + rebase
- Tests: `test_curated_accounting.py` **21 passed**; `test_curated_drift.py` **9 passed**
- Fee fixtures verified: BUY 6666 → 99.99+18.00=117.99; BUY 7000 → 100.00+18.00=118.00
- XIRR hand fixtures: 0.1500 (1y); 0.0826 (irregular 3-flow)

**NOT done**

- API/worker fee journal writer, dividend derivation from CA × holdings, Celery drift job
- ~~Investor UI (SC6)~~ **done SC6**; Track B collection still off

## SC6 investor UI (leaf 1.5.1 / 1.5.2) — 23 Aug 2026

**Built**

- `/investments`, `/investments/[id]`, `/investments/[id]/orders`, `/watchlist`, `/fees`
- `ShowDetailsModal` (SC4 accounting labels), `NetWorthHeader`, `InvestmentActions`, `FeeFaq`
- Reuses SC5 `PlanHandoffPanel` + `MarketClosedModal` on Invest more / Exit / Rebalance
- Nav + vocabulary; graceful empty when ledger APIs absent; watchlist hits `GET /watchlist`
- Vitest: `lib/investments/__tests__/read-only.test.ts` — no execute / place_order
- Gates leaf-1.5.1 / 1.5.2 / node-1.5 checked; DECISIONS-SC SC6 ⚠ UNREVIEWED

**NOT done**

- Live investment/fee list APIs (fetch returns empty until routers land)
- ~~Pending-actions carousel polish / home modules (SC9)~~ API + card landed SC9; home modules / trending still open
- Watchlist toggle on Explore cards; moved_pct needs NAV (SC4 service)
- E2E Playwright against DRY_RUN fixtures equal to SC4 ledgers

## SC11 integrity hardening (leaves 1.8.1–1.8.4) — 23 Aug 2026

**Built (tests + hardening only; uncommitted)**

- 1.8.1 no-order: `test_explore_no_orders.py` **2 passed**
- 1.8.2 tenant: `curated_tenant.py` + `test_curated_tenant_isolation.py` **5 passed**
  (sole-tenant `user_id` filter; foreign principal cannot see cross-user rows)
- 1.8.3 perf: explore list documents N+1 avoided + p95 < 1s; **3 passed**
  (`test_explore_perf.py` 2 + catalog budget assert 1)
- 1.8.4 memory: `METRICS_BASKET_CHUNK = 50` chunked `compute_all_metrics`
- 1.8.5 accuracy: already green (21 accounting)

**NOT done (SC11 AC remainder)**

- Runbook publish/apply/drift/Track-B flip; cold bring-up proof for `/explore`
- Full load measurement over 2011→now history

## SC3 versions (leaf 1.2.1) — 23 Aug 2026

**Built**

- `baskfy_core.curated_versions` — sequential immutable publish draft, classify
  GENESIS/CHANGED/NO_CHANGE, `diff_holdings_vs_weights` (04 §5 value-delta floor),
  residual cash + top-up, desk-shaped order lines, `PublishSideEffects` description
- `baskfy_api.curated_versions` — persist version+constituents; ENGINE post +
  `REBALANCE_AVAILABLE` + `PENDING` rebalance state; apply-preview helpers (no plan/execute)
- Tests: core **24 passed** (`test_curated_versions.py`); API service **4 passed**
  (`test_curated_versions_service.py`)

**NOT done (sibling leaves)**

- Plan generation / desk `plan_id` (1.2.2); market-hours guard (1.2.3)
- Router wiring (parent); full DRY_RUN invest→publish→diff→apply loop (SC3 AC end-to-end)


## Tree 2 AC-closure (23 Aug 2026)

See `SC-AC-REPORT.md`. SIP Beat `cb-sip-reminders`, `POST /api/v1/cb/baskets`, pure dividends, basket PerformanceChart, explore-handoff Playwright spec, RUN-AND-TEST §8. OAuth still D3-blocked.


## Independent verification (session `baskfy-11`, 23 Aug 2026)

Every module above was marked ✅ by the session that wrote it. A second session, holding no
stake in the outcome, graded the same code from outside. It found defects that were **live at
`babcd0e`**, after SC12 had been declared green and a final report written.

That is the finding worth keeping, and it outlives every item under it: **green was produced by
the party being graded.** `M43.4` and `M44` both exist because somebody else went looking.

### What was live after the run declared itself finished

| | Defect | Where it stood |
|---|---|---|
| S1 | `scoped_sole_user_id` returned the sole tenant to every caller — both arms of the branch returned the same value, and `POST /auth/register` is ungated, so any registered account read, overwrote and deleted the operator's watchlist | Shipped by **SC11** under the message *"tenant isolation"*, with `test_scoped_sole_collapses_foreign_principal` asserting it. Fixed by `M43.4` |
| S2 | `resolve_sole_user_id` seeded the e2e account from the **request path** — an upsert, so a `GET /watchlist` created an `app_user` whose password is a published repo constant and reset it on every call, at ~205 ms of Argon2id | Fixed on `fix/sc-hardening` |
| S3 | All six `not_found` sites passed one argument to a two-argument helper, so every 404 on the catalog was a **500**. mypy had reported all six the entire time, in a lint gate that was red while the module was called green | Fixed on `fix/sc-hardening` |
| S4 | `visibility == "PUBLISHED"` appeared at exactly one line in the router. `cb_basket` has no owner column, so a PRIVATE basket was readable by slug, anonymously | Fixed on `fix/sc-hardening` |
| S5 | Six catalog `GET`s carried no authentication at all, against `02-scope-and-gating.md`'s "web login only" | Fixed on `fix/sc-hardening` |
| A1 | One version's weights were replayed across the whole window, so a momentum basket's published record was today's winners run over history they were never held through. Measured: **+15.6pp** median 5Y CAGR overstatement across 300 simulated baskets, overstated in 292 of them | Fixed on `fix/sc-hardening` |
| A2 | Two observations were enough to publish a **LOW** risk label — two consecutive +2% days read as calm, because a sample standard deviation measures dispersion, not magnitude | Fixed on `fix/sc-hardening` |
| A3 | Windows were bar counts indexed off whatever dates existed: "1Y" landed 378 calendar days back, "5Y" spanned 5.12 years while `cagr()` was passed exactly 5. The repo's own `baskfy_core.windows` was never imported | Fixed on `fix/sc-hardening` |
| A4 | `money()` quantised NAV inside the daily loop — rounding at compute time on a number never stored — drifting up to ~41 bps over 1260 days | Fixed on `fix/sc-hardening` |
| A6 | Every catalog return is a **price** return (M39.3) and the cards said nothing | Fixed on `fix/sc-hardening` |

### Tests that asserted the bug

Three, which is why the suite stayed green through all of it:

- `test_scoped_sole_collapses_foreign_principal` pinned S1 by name.
- `test_another_user_id_cannot_see_sole_watchlist_rows` drove the handler as principal 99 and
  required the SQL to contain `42` and not `99` — the opposite of what its name says.
- `test_golden_nav_from_fixture_returns` asserted the per-day rounding intermediates, locking in
  the house-rule-8 violation rather than the spec.

All three were rewritten to assert the spec, per house rule 2. None was deleted.

### The structural gap

`test_explore_catalog.py` invoked handlers as plain coroutines — `await list_explore_baskets(...)`.
A router tested without an HTTP path cannot see its own dependency graph, so it cannot see that a
route has no authentication; and it never takes an error path, so it cannot see a 404 that is
really a 500. `test_explore_http.py` asserts at the boundary instead.

### Still open

- The pre-existing red listed under "Open items" below.
- The fragility probe reports **false fragility**. `_require_screen_coverage` checks the
  schedule and not the ±1 offsets the probe screens on; an uncovered offset yields an empty
  screen, and an empty screen liquidates to cash, so the offset runs diverge for a reason that
  has nothing to do with the strategy. `docs/10` primes the reader to expect fragility — "Most
  won't. That is the point." — so a data artefact is camouflaged as the documented expected
  outcome and nobody investigates it.

  **Two corrections to an earlier draft of this line, both from the backtesting session:**

  1. It said `blind_fraction` was "never serialised". Wrong: M43 put `blind_pct` in the
     fragility payload. What is missing is the wire and the template — it appears in neither
     `packages/api-client/src/generated/schema.ts` nor `fragility-panel.tsx` (0 occurrences in
     each, checked), so it still does not reach a human.
  2. It quoted **98.7%**, measured against the pre-M45 guard, and then **100% of 7** after I
     re-measured against the new one. Both are superseded. The backtesting session re-measured
     independently, checking *each* neighbour rather than whether either was missing, monthly
     over 2021-08-02→2026-12-31 on the live database:

     | | |
     |---|---|
     | monthly rebalance dates in span | 66 |
     | pass the M45 guard | 15 |
     | of those, at least one offset blind | **15 (100%)** |
     | of those, **both** offsets blind | **15 (100%)** |

     Not one neighbour missing — both, on every runnable date. My measurement could not have
     told those apart, because I only asked whether either was missing. Their figure stands.

  **And the cause changes what this is.** `factor_daily` and `index_member_daily` are **weekly**
  series; the dominant gap between sampled dates is five sessions. A neighbouring trading day has
  no factor rows *by construction*. So `docs/10`'s ±1 trading-day probe is asking the data plant
  for something it does not produce, and no widening of the coverage guard can satisfy it. This
  is a `docs/10` problem, not a `backtest.py` one, and it does not close when the backfill
  finishes.

  Owner: the backtesting tree, not this one, and it is on that session's list.
