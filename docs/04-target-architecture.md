# 04 — Target architecture

## 1. The shape

```
                          ┌──────────────────────────────────────────┐
   Browser  ────────────► │  apps/web — Next.js 15                   │
                          │  screens · baskets · desk · backtests    │
                          └───────────────────┬──────────────────────┘
                                              │ HTTPS + JWT
                                              ▼
                          ┌──────────────────────────────────────────┐
                          │  services/api — FastAPI                  │
                          │  /screens /baskets /plans /execute /ops  │◄── Redis
                          └───────┬──────────────────────┬───────────┘
                                  │                      │
              ┌───────────────────▼──────┐   ┌───────────▼─────────────────┐
              │ packages/core  (PURE)    │   │ packages/execution          │
              │ factors · screener       │   │ gateway → guards → risk     │
              │ score · basket · costs   │   │ → ratelimit → journal       │
              │ exposure (R1–R4)         │   │ THE ONLY ORDER PATH         │
              │ instrument_regime        │   └───────────┬─────────────────┘
              │ backtest (PIT)           │               │
              └──────────────────────────┘               │
                                  │                      │
                          ┌───────▼──────────────────────▼───────────┐
                          │ PostgreSQL 16 + TimescaleDB              │
                          │ ohlcv_daily · factor_daily · membership  │
                          │ screens · baskets · plans · orders       │
                          │ fills · snapshots · exposure · users     │
                          └───────▲──────────────────────────────────┘
                                  │
              ┌───────────────────┴───────────────────┐
              │ services/worker — Celery + Beat        │
              │ nightly: ingest → adjust → factors →   │
              │ publish · EOD snapshots · backtests    │
              └───────────────────┬───────────────────┘
                                  │
        ┌─────────────────────────┴───────────────────────────┐
        │ packages/providers                                  │
        │ MarketData: Kite (system acct) + NSE + composite     │
        │ Trading:    Kite (per-user acct, encrypted token)    │
        └─────────────────────────────────────────────────────┘
```

## 2. The two laws

Both are inherited, both are already enforced by tests on their own side, and both must be
enforced across the merged tree from day one.

> **Law 1 — `packages/core` touches nothing.**
> DataFrames in, DataFrames out. No database, no network, no disk, no clock. Anything that
> touches I/O belongs in `services/` or `packages/providers`.
> *(Decile's `CLAUDE.md`; the desk's `core/regime.py` is already written to it.)*

> **Law 2 — `packages/execution` is the only path to an order.**
> Guards → risk → rate limit → journal → broker. Nothing calls `kc.place_order` directly.
> Guards refuse untouchable instruments **before any network call**.
> *(the desk's rule 6a, which earned itself the hard way.)*

Law 2 gains a multi-tenant clause: **every order carries a `user_id` and a `broker_account_id`,
and the gateway refuses any order whose plan was built for a different one.**

## 3. Module map — where everything lands

| Today | Target | Note |
|---|---|---|
| `decile_core/factors.py`, `screener.py`, `factor_registry.py`, `blends.py`, `windows.py`, `precision.py`, `adjustments.py` | `packages/core/` | unchanged; the data plant's brain |
| `decile_core/regime.py` | `packages/core/instrument_regime.py` | **renamed** (§03 3c) |
| `decile_core/rebalance.py` | `packages/core/rank_buffer.py` | demoted to one input of the basket builder |
| `decile_core/backtest.py`, `backtest_metrics.py` | `packages/core/backtest/` | unchanged |
| `decile_core/entitlements.py`, `gst.py`, `invoice.py`, `pdf.py` | `packages/core/billing/` | unchanged |
| **`app/scoring.py`** | `packages/core/score.py` | already pure; only the `__main__` block moves |
| **`app/rebalance.py`** | `packages/core/basket.py` | **make pure** — lift `data/sectors.csv` read to a caller-supplied mapping |
| `app/costs.py` | `packages/core/costs.py` | already pure |
| `app/core/regime.py`, `regime_alloc.py` | `packages/core/exposure/` | already pure by design |
| `app/core/guards.py`, `risk.py`, `ratelimit.py`, `gateway.py` | **`packages/execution/`** | new package; add tenancy |
| `app/core/ticker.py`, `net.py` | `packages/providers/` | |
| `app/kite_client.py` | `packages/providers/kite_trading.py` | joins `kite.py` (market data) under one broker port |
| `app/analytics/*` (26 modules) | `services/api/desk/` + `packages/core/` where pure | biggest mechanical split |
| `app/main.py` + 13 templates | `services/api/routers/desk.py` + `apps/web/(app)/desk/*` | Jinja stays live during migration (§03 3d) |
| `scripts/daily.py`, `autorun.py`, `backup.py` | `services/worker/tasks/desk/` | Celery Beat replaces systemd timers |
| `app/strategies/strangle/*` | **frozen — not migrated** | §03 4 |
| `app/strategies/options*.py` | **frozen — not migrated** | |

## 4. The data model, joined

Decile's schema is the base. The desk's tables arrive with a `user_id` and a
`broker_account_id`, and three of them change shape:

| Desk table | Becomes | Change |
|---|---|---|
| `snapshots` | `portfolio_snapshot_daily` | + `user_id`; hypertable |
| `fills`, `trades` | `fill`, `trade` | + `user_id`, + `broker_account_id` |
| `rebalance_versions` / `rebalance_orders` | `plan` / `plan_order` | + `user_id`, + `basket_id`, + `screen_run_id` (**new — the input becomes reproducible**) |
| `regime_evaluations` / `regime_exposure` | `exposure_evaluation` / `exposure_allocation` | + `user_id`; the *market* signal stays global, the *allocation* is per user |
| `breadth_readings` | **deleted** | superseded by Decile's `market_health_daily`, which computes breadth from the full universe instead of from whatever CSV was to hand |
| `index_series`, `benchmark` | `index_snapshot_daily`, `benchmark_daily` | merge into Decile's equivalents |
| `corporate_actions` | `corporate_action` | Decile's is richer; the desk's 1 manual row migrates |
| `settings`, `settings_audit` | `user_setting`, `setting_audit` | + `user_id`; **system risk knobs excluded** (§03 3f) |
| `ops_jobs`, `daily_runs` | `job_run` | unified with Celery task records |
| `option_*` | **not migrated** | |

**The new join that did not exist before:** `plan.screen_run_id`. Today a rebalance plan
references a CSV filename. After the merge it references the exact screen definition, `as_of`
date and `data_version` that produced it — which is what makes a past decision reproducible and
a strategy backtestable end to end. That single column is arguably the biggest product win in
the whole merge.

## 5. Naming and namespace

One decision, taken once, before Phase 1 (see `06-decisions-required.md` D1). Assuming
**Baskfy**:

| | Value |
|---|---|
| Repo root | `baskfy` |
| Python namespaces | `baskfy_core`, `baskfy_api`, `baskfy_worker`, `baskfy_providers`, `baskfy_execution` |
| TS packages | `@baskfy/web`, `@baskfy/api-client` |
| Env prefix | `BASKFY_` |
| Database | `baskfy` |
| Domains | `baskfy.in` (product) · `desk.modelbasket.in` (existing operator console, unchanged) |

Decile's `docs/14` brand vocabulary is worth keeping even under a new name — **D1**, *decile
drift*, *Market Pulse*, *Replay*, the *hold band*. It is teachable in one sentence and it makes
the rank-buffer rule feel native. Rename the product, keep the language.

The rename is ~1,200 files of mechanical `sed` plus an Alembic migration. **It is cheap now and
expensive after three more modules.** Do it in Phase 0, as a single commit, with no other change
in it.

## 6. Deployment target

| | Today | Target |
|---|---|---|
| Desk | 1× Lightsail 4 GB Mumbai, systemd, Caddy, SQLite | unchanged through Phase 3 |
| Merged system | — | Mumbai (`ap-south-1`, for the Kite RTT floor and the static IP). API + worker containers, managed Postgres+Timescale, managed Redis, Cloudflare R2, Caddy or ALB |
| Static IP | one, allowlisted for one account | **open question** — per-user API keys on a shared egress IP needs a conversation with Zerodha (`06` D6) |

Decile's observability stack (OTel + Sentry + Prometheus + Grafana + 5 runbooks) arrives with it
and gets applied to the desk's paths, which currently have `journalctl` and nothing else. The
five runbooks acquire a sixth: **"a rebalance half-executed."**
