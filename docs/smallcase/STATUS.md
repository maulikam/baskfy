# SC run — live status

The status page for the smallcase-layer run. Updated at the end of every module, loud
about what is NOT done. A fresh session resumes from the first module not marked ✅.

**Run state: SC2 green · unlazy tree active · continuing to SC3.** Started 23 Aug 2026.

## Module ledger

| Module | State | One line |
|---|---|---|
| SC0 — Baseline and read-in | ✅ | Baselines recorded; desk 1328 passed; DRY_RUN=true; M41 catalog uncommitted at start |
| SC1 — Schema and domain objects | ✅ | 18 `cb_*` tables + migration 0014; domain asserts; manager seed; tests green |
| SC2 — Catalog computation and API | ✅ | metrics + explore API + Beat job + SCAN seed; gates/leaf-1.1.* |
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

**No curated-basket / metrics Beat entry yet** — SC2 adds the EOD `cb_metrics` job.

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
- Track B flags and unreachable-surface tests (SC10)
- `BASKFY_SOLE_USER_ID` resolver exists but no `cb_*` user rows written until SC4

## Open items / things a future session must know

1. **D3 still unanswered** (`NEEDS-MAULIK` item 13). Track C stays forbidden: no web execute, no third-party broker OAuth, no payment collection.
2. **M41 was uncommitted when SC0 started**; commit it before SC1 so the working tree only carries SC work.
3. Full screener suite was not re-timed this session — treat desk green + M41/nav green as the SC0 safety bar; expand before SC12.
4. ~~`docs/smallcase/03` `cb_*` schema does not exist in Alembic yet — SC1's job.~~ **Done (0014).**
5. Legacy `/baskets` vs explore catalog collision is deferred to SC5 (do not invent a second MomentumScan basket page in SC1–SC2 without recording the mapping).

## SC2 deliverables (23 Aug 2026)

- `baskfy_core.curated_metrics` — min_amount, vol buckets (PACK.1), returns/CAGR, chain-link NAV
- `baskfy_core.scan_projection` — deterministic SCAN → genesis version
- `GET /api/v1/explore*` + watchlist CRUD (`routers/explore.py`); **no execute**
- Celery Beat `curated_metrics` EOD upsert into `cb_metrics`
- Fixture SCAN basket seed via `curated_seed`
- Unlazy plan: `PLAN.md` + `gates/` (tree covering SC2–SC12 + integrity)

**NOT done:** SC3–SC12; full CAGR history over real bars in metrics job may be stubbed to
min_amount+vol from constituents where history is thin; web `/explore` UI is SC5.
