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

## Laya sidecar (25 Sep 2026) — out of the locked stack, and kept out of it

**What.** `convaiinnovations/laya` (Apache 2.0; PyPI `laya` 0.3.20; torch 2.14 CPU, transformers
5.x, a 421M-parameter ModernBERT checkpoint) reads the newest exchange headline of each scan
candidate and answers one typed `choice` question — what kind of corporate event the filing
announces — with a calibrated probability. `baskfy_core.catalyst_tags` resolves that answer
against the rules baseline and `GET /overlap` serves the result as display context on
`/build/overlap`. Maulik asked for it in session ("let use laya use as soon as possible").

**Why it is not a dependency of this stack.** House rule 1 locks the stack for the reasons §"Why
this" gives; torch alone is larger than every other wheel in `uv.lock` together, and a model
checkpoint is not a package. So the sidecar is **its own container on the stock `python:3.12`
image** (`infra/docker/compose.prod.yml`, service `laya`; loop in `infra/laya/laya_loop.py`)
with three runtime dependencies and no import from `packages/` or `services/`. The product's
whole contract with it is a Redis key and a JSON shape, both defined in
`baskfy_core.catalyst_tags` (`cache_key`, `tag_from_laya`) and restated in the loop. Nothing in
`uv.lock`, `pyproject.toml` or any image changes.

**Boundaries.** Read-only over `sw_catalyst`; writes only to Redis with a 30-day TTL; reads by
`baskfy_api.overlap` only; never by any rank, filter, size or order path
(`services/api/tests/test_overlap_readonly.py`). With the service down or the cache cold the
page shows the rules tag. `mem_limit: 3g` so a CPU model cannot take the box with it.

**Measured before adoption** (this checkpoint, zero-shot, CPU): event type right at 0.90–0.99 on
unambiguous headlines, wrong or below 0.60 on ambiguous ones — hence `LAYA_CONFIDENCE_FLOOR`
and the rules fallback. The "does this filing explain the pattern?" question answered
`probably_unrelated` at 0.21 on a ₹840 crore order under a gap and is not asked until a
fine-tuned checkpoint exists. About 1.3 s per headline on this CPU, batched.

**Pinned, twice (OV11, 26 Sep 2026).** The container used to `pip install "laya==0.3.20" …` on
every cold start and let pip resolve torch and transformers afresh, and `laya.load(repo)` resolved
the checkpoint's moving head. Now `infra/laya/requirements.txt` pins every wheel (compiled with
`uv pip compile` for `aarch64-unknown-linux-gnu` / Python 3.12 against the CPU torch index, the box
being a t4g.large, and checked identical to the venv the box built on 25 Sep — the
recipe is in the file's header; re-compile, never hand-edit), compose installs from that file
alone, and the loop downloads exactly `LAYA_MODEL_REVISION` (a Hugging Face commit sha, default the
one the box first loaded) and hands laya the local directory. The model tag is part of every cached
answer and a change of it clears the sidecar's keys. `uv.lock` is still untouched: the pin lives
beside the sidecar, out of the stack.

**Corrected (OV11).** "Calibrated probability" above was true of what laya *returns* and false
of what the sidecar *stored* until 26 Sep 2026: it cached laya's `confidence`, which on a `choice`
question is a normalised-entropy score, and the readers gated on it. The payload's `confidence` is
now `answer_confidence`, the probability of the chosen answer; the entropy score is kept under its
own name; cache keys are `v2` with the question-schema hash so no old answer is read against the
floor. `docs/07a` §17 has the account.

**Reverse.** Remove the `laya` service and volume from compose, the `laya` tokens in
`tools/deploy/{deploy-swing,ship}.sh`, `infra/laya/`; the API then serves the rules tag alone.
