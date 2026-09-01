# 01 — Architecture of the Go tree

## Layout (one module: `github.com/maulikam/baskfy`, rooted at `go/`)

```
go/
  go.mod  go.sum  Makefile  sqlc.yaml  oapi-codegen.yaml  .golangci.yml
  cmd/
    api/        main.go            # baskfy-api      :8001 (Python API stays on :8000 during strangler)
    worker/     main.go            # baskfy-worker   scheduler + job runner
    desk/       main.go            # baskfy-desk     :8421 (Python desk stays on :8420)
    baskfy/     main.go            # CLI: pipeline, backfill, factors, kite-session, parity, seed
  internal/
    domain/     # L0 — the shared vocabulary. FROZEN after G0; additions only via REQUESTS.md
    config/     # L0 — every BASKFY_* / desk env var, one struct, same names as .env.example
    db/         # L0 — pgx pool, tx helpers, schema.sql (dump), queries/*.sql per lane, gen/ (sqlc)
    obs/        # L0 — slog JSON, OpenTelemetry, Prometheus, Sentry
    testkit/    # L0 — golden loader, test DB (BASKFY_TEST_DATABASE_URL), fixture readers
    core/       # THE FIRST LAW: imports nothing but domain, stdlib math/sort/time-types, gonum, decimal
      signals/    # L1  factors, score, momentum(_scan), basket(+sizing), screener, screen_definition*, rank_buffer, windows, universes, breadth, precision, trading_calendar, circuits, costs, blends, factor_registry, instrument_regime, risk_free, scan_projection, screen_diff
      backtest/   # L2  backtest, backtest_metrics
      regime/     # L2  exposure/regime, exposure/allocation
      actions/    # L2  adjustments, action_recovery
      ledger/     # L2  reconcile, reconciliation, sleeves, allocation_ledger, cash_ledger
      curated/    # L3  curated_* (10 files), entitlements, manager_onboarding, sebi_registration
      portfolio/  # L3  portfolio_csv, portfolio_graph, portfolio_nav, portfolio_units, cas_import, grouping_suggestions, pros_cons
      billing/    # L3  gst, invoice
      accounts/   # L3  api_keys, broker_connections, tenancy, public_api, seed_data
    providers/  # L4  kite, nse, composite, ratelimit, tokens (fernet), circuit, retry, archive (R2), records, fixtures, ports
    execution/  # L4  THE SECOND LAW: gateway, guards, risk, ratelimit, journal, tenancy, broker_ports, adapters
    api/
      server/     # L5  oapi-codegen output (strict server + types) — generated, committed, never hand-edited
      app/        # L5  router mount, middleware chain, problems (RFC 7807), csrf, security, ratelimit, idempotency, http_cache, settings
      auth/       # L5  jwt (HS256), argon2id, google, auth_service, routers/auth
      platform/   # L5  admin, api_keys, billing, razorpay, invoices, webhooks, webhook_endpoints, alerts, email (resend + templates), meta, public, public_docs, support, track_b, managers, entitlements
      market/     # L6  screens, explore, instruments, market_data, search, screener, query_plans, csv_export, integrity, metrics
      backtests/  # L6  backtests, backtest_runner
      portfolios/ # L6  baskets, portfolios, sleeves, portfolio_overview
      curated/    # L7  curated_catalogue, curated_investments, curated_metrics_service, curated_seed, curated_tenant, curated_versions + the 10 routers/curated_*
      brokers/    # L7  brokers, broker_accounts, broker_holdings, broker_oauth, kite, kite_basket
      desk/       # L7  routers/desk, desk_schema
    worker/     # L8
      scheduler/  # 18 Beat entries → robfig/cron, IST
      jobs/       # River queue: enqueue/consume; job = former Celery task
      pipeline/   # orchestrator, steps, engine, window, ops
      backfill/   # backfill, deep_backfill, bhavcopy_backfill, breadth_backfill, index_backfill, reference_backfill, calendar
      tasks/      # tasks/* (26 files)
      cli/        # factors_cli, fundamentals_cli, kite_session_cli, pipeline_cli, reconcile_cli, cb_metrics_cli
    desk/       # L4 (wave 2–3): main.py routes, analytics/*, config, rebalance, scan_source, breadth_source, telemetry
  web/desk/     # L4  templates/*.html → html/template (embed), static/ copied verbatim
  testdata/golden/L1 … L8/   # JSON goldens dumped from Python (see 03)
```

**Ownership is by directory.** A lane writes only under its directories plus
`internal/db/queries/L<n>_*.sql`, `testdata/golden/L<n>/`, `docs/go-rewrite/status/L<n>.md`,
and `tools/parity/dump_L<n>*.py`. Anything else is a `REQUESTS.md` entry.

## Locked stack (the Go `docs/02-tech-stack-adr.md`)

| Need | Python today | Go | Why this one |
|---|---|---|---|
| HTTP + routing | FastAPI | `net/http` + `github.com/go-chi/chi/v5` | stdlib-compatible; oapi-codegen has a first-class chi strict-server target |
| API contract | Pydantic models | `github.com/oapi-codegen/oapi-codegen/v2` from `packages/api-client/openapi.json` | the contract already exists; the web app is generated from the same file |
| Postgres | SQLAlchemy 2 async + asyncpg / psycopg | `github.com/jackc/pgx/v5` + `github.com/sqlc-dev/sqlc` | typed queries from the real schema; no ORM to mis-model 89 tables |
| Migrations | Alembic | **none tonight** (Alembic stays). Later: `pressly/goose` with a baseline | schema is a contract |
| Money / prices | `Decimal`, `numeric` | `github.com/shopspring/decimal` | house rule 9: never float for money |
| Numerics | polars + numpy + pandas | typed slices + `gonum.org/v1/gonum` (stat, mat, floats) | no DataFrame library — see §Frames |
| Redis | redis-py | `github.com/redis/go-redis/v9` | **key formats stay identical** to Python's (rate limits, screen cache, Kite token bucket) so both stacks coexist |
| Queue | Celery + Beat | `github.com/riverqueue/river` (Postgres-backed) + `github.com/robfig/cron/v3` | transactional enqueue in the same DB; no Celery protocol emulation. River's tables are the *one* schema addition and live in their own `river` schema (record in DECISIONS-GO G8.1) |
| JWT | PyJWT HS256 | `github.com/golang-jwt/jwt/v5` | same secret/issuer/audience; tokens minted by Next.js keep verifying |
| Passwords | argon2-cffi | `github.com/alexedwards/argon2id` | same PHC string format; existing hashes verify |
| Token encryption | cryptography Fernet | `github.com/fernet/fernet-go` | reads the existing encrypted token store |
| Kite Connect | kiteconnect 5.x | `github.com/zerodha/gokiteconnect/v4` | official client; **order methods are never called outside `internal/execution/adapters`** |
| NSE | httpx + cookie jar | `net/http` with `cookiejar`, same header discipline as `nse.py` | rule: no scraping around the provider |
| R2 archive | boto3 | `github.com/aws/aws-sdk-go-v2` (S3 API, R2 endpoint) | |
| Email | resend | `github.com/resend/resend-go/v2` | |
| Payments | razorpay | `github.com/razorpay/razorpay-go` | webhook signature = HMAC-SHA256 as today |
| PDF (invoices) | reportlab? (`pdf.py`) | `github.com/go-pdf/fpdf` | |
| Templates (desk) | Jinja2 | `html/template` + `embed` | |
| Observability | OTel + prometheus_client + sentry | `go.opentelemetry.io/otel`, `github.com/prometheus/client_golang`, `github.com/getsentry/sentry-go`, `log/slog` | same metric names as today (`services/api/metrics.py`, desk `telemetry.py`) |
| Tests | pytest + hypothesis | `testing` + `github.com/stretchr/testify` + `pgregory.net/rapid` (property tests) | |
| Lint | ruff + mypy strict | `golangci-lint` (errcheck, staticcheck, govet, revive, gosec, depguard) | `depguard` is how the two laws are enforced |

Nothing outside this table without a `DECISIONS-GO.md` entry first (house rule 1).

## The two laws, in Go form (enforced by `golangci-lint depguard` + a test, not by discipline)

1. **`internal/core/**` imports nothing that does I/O.** Allowed imports: stdlib except
   `net/*`, `os`, `io/fs`, `database/sql`, `time.Now` (a `domain.Clock` is passed in);
   `internal/domain`; `gonum`; `decimal`. `go/internal/core/laws_test.go` walks the import graph
   and fails on a violation — the Go `test_no_escape_hatches.py`. Also banned there: `panic`
   outside `init`, `_ = err`, `interface{}`/`any` in exported signatures.
2. **`internal/execution` is the only importer of `gokiteconnect`'s order/GTT methods.**
   `depguard` forbids `gokiteconnect` everywhere except `internal/providers/kite` (data only:
   instruments, quotes, historical, holdings, positions, margins) and `internal/execution/adapters`.
   The gateway order is fixed: guards → risk → rate limit → journal → broker. `client_id =
   plan_id:symbol`. Every order carries `user_id` + `broker_account_id`; mismatch refuses.
   **Tonight the only `Broker` implementation that compiles is `DryRunBroker`.** A GTT path
   exists in the gateway from day one (closing the M16 caveat), also dry-run only.

## Frames: what replaces DataFrames

Do **not** build a DataFrame library. `packages/core` is "frames in, frames out"; in Go that is
**typed slices in, typed slices out**, with three shared shapes in `internal/domain`:

```go
type Bar struct { Symbol Symbol; Date Date; Open, High, Low, Close, CloseRaw decimal.Decimal; Volume int64 }  // close adjusted, close_raw exchange print (house rule 6)
type Series struct { Dates []Date; Values []float64 }                      // one symbol, one column, dense, ascending
type Panel  struct { Dates []Date; Symbols []Symbol; M *mat.Dense }         // dates × symbols, NaN = missing; the polars pivot
```

Rules: factor math is `float64` inside a function and **rounds at the boundary** exactly where
Python rounds (house rule 8 — "storage precision is the contract"; `precision.py` is the table);
money and prices are `decimal.Decimal` at every boundary and in every struct that reaches a
table. Point-in-time membership is an explicit `MembershipAt(date)` argument, never a filter
inside a factor (house rule 5). Missing data is `math.NaN()` in `float64` panels and `nil`
pointers / `sql.Null*` at the DB edge — never zero.

## Conventions

- Package names are the Python module names, singular, lower-case (`signals`, not `signal`;
  `curated`, not `curatedbaskets`). Exported identifiers keep the Python names in Go casing
  (`stop_from_vol` → `StopFromVol`, `apply_filters_on` → `ApplyFiltersOn`) so `grep` across the
  two trees works during review.
- Every Python file maps to **one** Go file with the same stem (`factors.py` → `factors.go`,
  test `factors_test.go`); larger splits keep the stem as a prefix.
- Errors: wrap with `fmt.Errorf("signals.Score: %w", err)`; sentinel errors exported per package;
  no swallowed errors (`errcheck` is on, `_ = err` is banned).
- Context first argument everywhere outside `internal/core`. `internal/core` takes a
  `domain.Clock` where Python used `date.today()`/`datetime.now()`.
- Config: `internal/config.Load()` reads the **same** env names as `.env.example` (137 of them)
  and the desk's `config.py`; unknown-but-set `BASKFY_*` names are logged at startup so drift is
  visible. No new env names tonight.
- IST: `domain.IST = time.FixedZone("IST", 5*3600+1800)`; every schedule and calendar uses it.
- Logging: `slog` JSON with the same field names as `services/api/logging.py`
  (`request_id`, `user_id`, `route`, `duration_ms`).
- Commit messages: `G<lane>.<k>: green — <one sentence>` (see 04).
