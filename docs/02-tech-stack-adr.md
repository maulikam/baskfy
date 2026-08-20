# 02 — Architecture Decision Record: the locked tech stack

Status: **ACCEPTED**. These decisions are inputs to every prompt in `PROMPTS.md`. Do not change
them mid-build without re-reading the prompts that depend on them.

---

## The decision in one table

| Layer | Choice | Version |
|---|---|---|
| Web app | **Next.js (App Router) + React + TypeScript** | Next 15, React 19, TS 5.6 |
| Styling | **Tailwind CSS v4 + shadcn/ui + Radix** | — |
| Tables | **TanStack Table v8 + TanStack Virtual** | — |
| Charts | **visx** (or Recharts for simple cards) | — |
| Client data | **TanStack Query v5** + `nuqs` for URL-encoded filter state | — |
| Auth | **Auth.js v5** (credentials + email OTP), sessions in Postgres | — |
| API | **FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + Alembic** | Python 3.12 |
| Numerics | **Polars** (primary) + NumPy; pandas only at boundaries | — |
| Database | **PostgreSQL 16 + TimescaleDB** (hypertable for daily bars) | — |
| Cache / queue broker | **Redis 7** | — |
| Jobs | **Celery + Celery Beat** (workers), `flower` for inspection | — |
| Object storage | **S3-compatible (Cloudflare R2)** for exports + raw file archive | — |
| Payments | **Razorpay** (subscriptions + one-time for Forever) | — |
| Email | **Resend** (transactional) | — |
| Observability | **OpenTelemetry → Grafana/Tempo/Loki**, **Sentry** for errors | — |
| Tests | **pytest + hypothesis** (Python), **Vitest** (unit TS), **Playwright** (e2e) | — |
| CI/CD | **GitHub Actions** → GHCR images → deploy | — |
| Runtime | **Docker Compose on a single Hetzner CCX** (scale-up first), Traefik TLS | — |

---

## Why this, and not the alternatives

### Why a Python service at all, instead of all-TypeScript

The product *is* a numerical engine with a web skin. Every headline feature — 62 ranking
factors, rolling volatility, beta regression, RSI, decile bucketing, a combined-rank sort, and
(from Dec 2026) a vectorised backtest over 15 years × ~2,000 instruments — is columnar array
math. In Polars/NumPy the nightly factor build for the full NSE universe is a handful of
vectorised expressions and runs in seconds; the same code in Node is either a hand-rolled loop
(slow, error-prone) or a thin binding to the same native libraries anyway.

Rejected: all-TypeScript. It optimises for one language at the cost of the part of the system
that is hardest to get right and most expensive to get wrong.

Rejected: Django monolith. Excellent for the CRUD, but the results grid needs virtualised,
column-configurable, 3,500-row client interactivity, and the marketing/SEO pages want SSR.
HTMX can do it; it just fights you.

Rejected: FastAPI + Vite SPA. Loses SSR/SEO for the public pages (pricing, blog, instrument
pages are exactly the kind of long-tail SEO surface that acquires users for a tool like this).

### Why Postgres + TimescaleDB, not ClickHouse / DuckDB / Parquet

Daily bars for ~2,300 NSE instruments × 15 years ≈ 8.5M rows. That is small. A Timescale
hypertable with a `(instrument_id, date)` composite index and native compression on chunks
older than 90 days keeps the whole working set in memory on a modest box, while keeping
screens, users, billing and factors in the *same* transactional database. One database, one
backup story, one migration tool.

TimescaleDB is a strict addition, not a lock-in: every query is plain SQL and degrades to
vanilla Postgres if the extension is dropped.

Revisit ClickHouse only when intraday bars or per-user backtest fan-out exceed ~10^9 rows.

### Why precomputed factors, not compute-on-request

62 factors × 5 windows × 2,300 instruments, recomputed per screen request, is the difference
between a 40 ms screen and a 4 s screen. The nightly job materialises `factor_daily` — one wide
row per instrument per date. A screen is then a single indexed `SELECT … WHERE … ORDER BY`.
Historical ranks and backtests read the same table, which is precisely what makes them cheap.

### Why Celery, not Prefect/Airflow/Dagster

The pipeline is ~8 tasks with a linear dependency chain, run once a day. Airflow-class tooling
is more operational surface than the job graph justifies. Celery Beat + a `pipeline_run` table
with explicit step states gives us retries, idempotency and a run history in ~200 lines.

### Why Kite (Zerodha) as the primary data source, and why it is not sufficient alone

Kite Connect gives clean, reliable adjusted/unadjusted daily candles per instrument. It does
**not** give: index constituents, index PE/PB/DivYield, the corporate-action calendar, series
codes for the full universe, or the listings history. Those come from NSE public files.

Therefore: a **`MarketDataProvider` port** with `KiteProvider` (bars), `NSEProvider`
(constituents, corp actions, indices, listings) and a `CompositeProvider` that routes by
capability. Adding a paid vendor later is a new adapter, not a rewrite.

Constraints to design around (verify against current Kite docs at build time):
- ~3 requests/second; batch and back off.
- Historical candles are capped per request for `day` interval — chunk the backfill by year.
- Redistribution of broker-sourced data to third parties is licence-restricted. Serve
  *derived* analytics; keep raw bars server-side. Legal review before any public data API.

### Why Razorpay

Domestic INR, UPI/netbanking/cards, subscriptions plus one-time payments (needed for the
"Forever" plan), GST-compliant invoicing. Stripe's India support does not cover the same
domestic rails as cleanly.

---

## Repo layout (monorepo, pnpm workspaces + uv)

Product name: **Decile** (see `docs/14-brand-and-naming.md`).

```
decile/
├─ apps/
│  └─ web/                  # Next.js 15
├─ services/
│  ├─ api/                  # FastAPI  (app/, alembic/, tests/)
│  └─ worker/               # Celery tasks (imports the api package)
├─ packages/
│  ├─ core/                 # Python: domain, factors, screener, backtest  (pure, no I/O)
│  ├─ providers/            # Python: Kite / NSE / composite adapters
│  └─ api-client/           # TS client generated from OpenAPI
├─ infra/
│  ├─ docker/  compose.yml  compose.prod.yml
│  └─ migrations/
├─ docs/                    # this bundle
└─ .github/workflows/
```

`packages/core` is deliberately I/O-free: it takes DataFrames in and returns DataFrames out.
That is what makes the factor math unit-testable and the backtest engine reusable.

---

## Non-negotiable engineering rules for the whole build

1. **No look-ahead, ever.** Every query that references a past date must use point-in-time
   index membership and point-in-time factor rows. This is asserted by tests, not by discipline.
2. **Adjusted by default.** `close` is adjusted; `close_raw` is the exchange print. Factors use
   `close`. Display uses `close_raw` where the user expects a real price.
3. **Idempotent ingestion.** Re-running any day's job produces identical rows (upserts keyed on
   `(instrument_id, date)`).
4. **Every derived number is reproducible.** A CLI can recompute any factor for any instrument
   on any date and print the intermediate series.
5. **Typed end to end.** Pydantic models → OpenAPI → generated TS client. No hand-written
   fetch types.
6. **Money and disclaimers are first-class.** The "not a SEBI registered investment adviser"
   disclaimer is a component rendered on every analytics surface, not a footer afterthought.
