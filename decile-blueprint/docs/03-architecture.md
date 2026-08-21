# 03 — System architecture

## Components

```
                         ┌───────────────────────────┐
   Browser  ───────────► │  Next.js 15 (apps/web)    │
                         │  SSR pages, Auth.js,      │
                         │  server actions, RSC      │
                         └────────────┬──────────────┘
                                      │ HTTPS, JWT (HS256, shared secret)
                                      ▼
                         ┌───────────────────────────┐
                         │  FastAPI (services/api)   │
                         │  /screens /results /...   │◄── Redis (result cache, rate limit)
                         └────────────┬──────────────┘
                                      │ asyncpg
                                      ▼
                    ┌──────────────────────────────────────┐
                    │ PostgreSQL 16 + TimescaleDB          │
                    │ ohlcv_daily (hypertable)             │
                    │ factor_daily, index_member_daily,    │
                    │ screens, users, subscriptions, …     │
                    └──────────────────▲───────────────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         │ Celery workers + Beat     │
                         │ (services/worker)         │
                         └─────────────┬─────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                │  providers: Kite (bars) · NSE (index,       │
                │  corp actions, listings) · Composite        │
                └─────────────────────────────────────────────┘
```

## Request path for a screen run (the hot path)

1. Browser hits `/screens/:id` (RSC). Next.js server component calls the API with the user's JWT.
2. API resolves the screen definition → builds a `ScreenQuery`.
3. Cache key = `sha256(screen_definition_json + as_of_date + data_version)`. Redis hit → return.
4. Miss → one SQL statement against `factor_daily ⋈ index_member_daily ⋈ instrument`:
   - universe filter (PIT membership),
   - `apply_filters_on` bucket (decile / top-N by marketcap within the universe),
   - all scalar filters as `WHERE` predicates,
   - ranking via `ROW_NUMBER() OVER (ORDER BY factor …)`, summed across 1–3 factors.
5. Result rows (≤ 4,000) serialised, cached with TTL until next EOD publish, returned.
6. Client renders with TanStack Table (virtualised), sorting/paging client-side.

Target: p95 < 150 ms warm, < 800 ms cold.

## Nightly pipeline (Celery Beat, 19:30 IST weekdays; NSE EOD files settle ~18:00–19:00 IST)

```
 1. refresh_instruments        (Kite instruments dump + NSE series/listing files)
 2. fetch_daily_bars           (Kite historical, per instrument, chunked, rate-limited)
 3. fetch_corporate_actions    (NSE; upsert into corporate_action)
 4. apply_adjustments          (recompute adj factors; rewrite close/open/high/low/volume)
 5. refresh_index_membership   (NSE constituent files → index_member_daily, PIT)
 6. refresh_index_snapshots    (index level, PE, PB, div yield → index_snapshot_daily)
 7. compute_factors            (Polars; writes factor_daily for the trade date)
 8. compute_market_health      (breadth per universe → market_health_daily)
 9. data_quality_gate          (assertions; abort + alert on failure)
10. publish                    (bump data_version, purge Redis screen cache, warm top screens)
```

Every step writes a row in `pipeline_run_step` with status, duration, row counts and an error
payload. Step 9 is a hard gate: if it fails, `data_version` is **not** bumped, so the site
continues serving yesterday's consistent snapshot rather than today's broken one.

## Environments

| Env | Data | Notes |
|---|---|---|
| `local` | 100-instrument sample, 3 years, seeded from fixtures | no Kite calls, provider stubbed |
| `staging` | full backfill, yesterday's data | real providers, test Razorpay keys |
| `prod` | full | daily backups + WAL archiving to R2 |

## Scaling plan (in order, only when measured)

1. Vertical: bigger box. 8.5M-row fact table stays memory-resident for a long time.
2. Read replica for the API; writers stay on primary.
3. Timescale continuous aggregates for market-health and index history.
4. Move backtest fan-out to a dedicated worker pool with its own queue.
5. Only then consider ClickHouse for backtest scratch space.
