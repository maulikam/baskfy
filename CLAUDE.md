# Decile — working agreement

India-equities momentum screener. `docs/` is the **source of truth**. If your instinct conflicts
with it, say so out loud rather than deviating quietly.

## House rules

1. Read `docs/02-tech-stack-adr.md` before proposing any dependency. Nothing outside the locked
   stack without saying why first.
2. Tests assert the **spec**, never current behaviour. If a test would only lock in what the code
   happens to do today, it is not worth writing.
3. No `# type: ignore`, no `any`, no silently swallowed exceptions.
   `packages/core/tests/test_no_escape_hatches.py` enforces this by scanning the source.
4. Every module ends with: tests passing, `make lint` clean, and a `docs/` update if behaviour
   diverged from the spec.
5. **No look-ahead, ever.** Anything referencing a past date uses point-in-time index membership
   and point-in-time factor rows. Asserted by tests, not by discipline.
6. **Adjusted by default.** `close` is adjusted; `close_raw` is the exchange print. Factors read
   `close`; display uses `close_raw` where the user expects a real price.
7. **Idempotent ingestion and seeding.** Re-running any day's job produces identical rows.
8. **Round at write time.** Storage precision is the contract (`docs/13` §4), so the API, the UI
   and the CSV export can never disagree.
9. Money and prices are `numeric`, never `float`. Disclaimers are components, not footers.

## Stack (locked — `docs/02-tech-stack-adr.md`)

| Layer | Choice |
|---|---|
| Web | Next.js 15 (App Router), React 19, TS 5.6, Tailwind v4, shadcn/ui |
| Tables / charts | TanStack Table v8 + Virtual, visx |
| Client data | TanStack Query v5, `nuqs` for URL filter state |
| Auth | Auth.js v5, sessions in Postgres |
| API | FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic, Python 3.12 |
| Numerics | Polars (primary) + NumPy; pandas only at boundaries |
| Database | PostgreSQL 16 + TimescaleDB |
| Cache / broker | Redis 7 |
| Jobs | Celery + Celery Beat |
| Storage / payments / email | Cloudflare R2, Razorpay, Resend |
| Tests | pytest + hypothesis, Vitest, Playwright |

## Layout

```
apps/web/                 Next.js 15                                    (Prompt 8)
services/api/             FastAPI + alembic/ + tests/                   (Prompt 7)
services/worker/          decile_worker — Celery, Beat, the ten nightly steps
packages/core/            decile_core — domain, factors, screener. NO I/O.
packages/providers/       decile_providers — Kite / NSE / composite / fixtures
packages/api-client/      @decile/api-client — Zod contracts, generated TS client
infra/docker/             compose.yml, initdb/
tests/fixtures/           the reference CSV export and shared corpora
docs/                     the specification
```

`packages/core` is deliberately I/O-free — DataFrames in, DataFrames out. Anything that touches a
database, a network or a disk belongs in `services/` or `packages/providers`.

## Where things live

| Looking for | It is in |
|---|---|
| Every table's model | `packages/core/src/decile_core/models/` |
| Storage precision decisions | `packages/core/src/decile_core/models/base.py` |
| The 14 universes and their mask bits | `packages/core/src/decile_core/universes.py` |
| `ScreenDefinition` (Pydantic) | `packages/core/src/decile_core/screen_definition.py` |
| **The screener query builder (pure)** | `packages/core/src/decile_core/screener.py` |
| Screen execution, as-of resolution, cache | `services/api/src/decile_api/screener.py` |
| Screener deviations from `docs/06` | `docs/06a-screener-implementation-notes.md` |
| FastAPI app, middleware, error handlers | `services/api/src/decile_api/app.py` |
| Endpoints (`/meta/*`, `/screens/*`, `/instruments/*`, `/indices/*`, …) | `services/api/src/decile_api/routers/` |
| Factsheet assembly, percentiles, medians | `services/api/src/decile_api/instruments.py` |
| Dashboard, breadth and listings queries | `services/api/src/decile_api/market_data.py` |
| RFC 9457 problem catalogue | `services/api/src/decile_api/problems.py` |
| JWT verification, entitlement stub | `services/api/src/decile_api/{auth,entitlements}.py` |
| **Argon2id, OTP, opaque tokens** | `services/api/src/decile_api/security.py` |
| Login, rotation, lockout, erasure | `services/api/src/decile_api/auth_service.py` |
| Double-submit CSRF + the auth cookies | `services/api/src/decile_api/csrf.py` |
| Email templates and transports | `services/api/src/decile_api/email/` |
| Log redaction (no secret ever printed) | `services/api/src/decile_api/logging.py` |
| Auth deviations from `docs/07`/`docs/11` | `docs/12a-auth-implementation-notes.md` |
| **The entitlement service (one place, server truth)** | `services/api/src/decile_api/entitlements.py` |
| Entitlement shape, tiers, `plan.features` parsing | `packages/core/src/decile_core/entitlements.py` |
| Razorpay client + every signature check | `services/api/src/decile_api/razorpay.py` |
| Checkout, webhook, subscription lifecycle | `services/api/src/decile_api/billing.py` |
| Invoice numbering, GST wiring, R2 storage | `services/api/src/decile_api/invoices.py` |
| GST arithmetic (pure) | `packages/core/src/decile_core/gst.py` |
| The tax invoice, as a document (pure) | `packages/core/src/decile_core/invoice.py` |
| The minimal PDF writer (pure, no dependency) | `packages/core/src/decile_core/pdf.py` |
| `/plans`, `/checkout/session`, `/webhooks/razorpay`, `/invoices` | `services/api/src/decile_api/routers/billing.py` |
| Billing tables not in `docs/04` | `packages/core/src/decile_core/models/billing.py` |
| `/pricing` and `/invoices` pages | `apps/web/src/app/(app)/{pricing,invoices}/` |
| Plan card, checkout button, invoice table | `apps/web/src/components/billing/` |
| Plan and invoice reads, price formatting | `apps/web/src/lib/billing/` |
| **Every Prompt 13 decision taken under ambiguity** | `docs/DECISIONS.md` §13 |
| **The rank-buffer rebalance rule (pure)** | `packages/core/src/decile_core/rebalance.py` |
| Portfolio CSV parser + the sample file (pure) | `packages/core/src/decile_core/portfolio_csv.py` |
| Symbol resolution, holdings, rebalance execution | `services/api/src/decile_api/portfolios.py` |
| `/portfolios`, `/import-csv`, `/rebalance`, history | `services/api/src/decile_api/routers/portfolios.py` |
| Rebalance history table (not in `docs/04`) | `decile_core.models.accounts.PortfolioRebalance` |
| The wizard, the three columns, the parse report | `apps/web/src/components/portfolios/` |
| Portfolio reads, clipboard and CSV payloads | `apps/web/src/lib/portfolios/` |
| **Every Prompt 14 decision taken under ambiguity** | `docs/DECISIONS.md` §14 |
| **The backtest engine (pure, point-in-time)** | `packages/core/src/decile_core/backtest.py` |
| The look-ahead guard (`PointInTimeReader`) | `packages/core/src/decile_core/backtest.py` |
| Backtest metrics and artefacts (pure) | `packages/core/src/decile_core/backtest_metrics.py` |
| The synthetic market the six correctness tests run on | `packages/core/tests/backtest_fixtures.py` |
| Panel loading, screen-per-rebalance-date, fragility | `services/worker/src/decile_worker/backtest.py` |
| The backtest job (claim, simulate, store, publish) | `services/worker/src/decile_worker/tasks/backtests.py` |
| Concurrency caps, artefact keys, signed links, payload | `services/api/src/decile_api/backtests.py` |
| `/backtests`, `/trades`, `/holdings`, `/export`, SSE | `services/api/src/decile_api/routers/backtests.py` |
| Enqueuing to Celery from the API | `services/api/src/decile_api/queue.py` |
| Config form, progress, results, fragility, assumptions | `apps/web/src/components/backtests/` |
| Backtest reads, the SSE reader, metric labels | `apps/web/src/lib/backtests/` |
| **Every Prompt 15 decision taken under ambiguity** | `docs/DECISIONS.md` §15 |
| Auth tables not in `docs/04` | `docs/04c-auth-tables-addendum.md` |
| HTTP rate limiting (docs/07 §Conventions) | `services/api/src/decile_api/ratelimit.py` |
| Streaming CSV export | `services/api/src/decile_api/csv_export.py` |
| `openapi.json` emitter | `services/api/src/decile_api/openapi.py` |
| Generated TS client (never hand-edit) | `packages/api-client/src/generated/schema.ts` |
| API deviations from `docs/07` | `docs/07a-api-implementation-notes.md` |
| **Design tokens (Tailwind v4 has no config file)** | `apps/web/src/app/globals.css` |
| App shell, sidebar, ⌘K, freshness pill | `apps/web/src/components/shell/` |
| DataTable, StatCard, FactorCombobox, … | `apps/web/src/components/data/` |
| Every primitive, rendered and measured | `apps/web/src/app/(app)/kitchen-sink/` |
| Auth.js v5, adapter, the `/auth/*` bridge | `apps/web/src/lib/auth/` |
| Gated-route redirect + the CSP nonce | `apps/web/src/middleware.ts` |
| Login, register, reset, verify pages | `apps/web/src/app/(auth)/` |
| Profile, change password, DPDP export | `apps/web/src/app/(app)/{profile,change-password}/` |
| Number and date presentation | `apps/web/src/lib/format.ts` |
| Sidebar IA (pinned to `docs/08` by a test) | `apps/web/src/lib/nav.ts` |
| Acceptance checks (Lighthouse, 60fps, CLS) | `apps/web/e2e/` |
| Web deviations from `docs/08` | `docs/08a-web-implementation-notes.md` |
| Screens list, editor, columns editor | `apps/web/src/app/(app)/screens/` |
| Filter form, results panel, peek drawer | `apps/web/src/components/screens/` |
| Filter groups, URL state, active counts | `apps/web/src/lib/screens/` |
| The 93-column reference CSV export | `services/api/src/decile_api/csv_export.py` |
| Screens-UI deviations from `docs/08`/`docs/01` §2 | `docs/09a-screens-ui-implementation-notes.md` |
| Instrument page, OG image, JSON-LD | `apps/web/src/app/(app)/instruments/[symbol]/` |
| Factsheet blocks (docs/01 §5) | `apps/web/src/components/instrument/` |
| Cell units, SEO copy, ISR fetch | `apps/web/src/lib/instrument/` |
| Factsheet deviations from `docs/01` §5 | `docs/10a-instrument-factsheet-notes.md` |
| Indices dashboard, gauges, listings table | `apps/web/src/app/(app)/{dashboard,market-health,listings}/` |
| Gauges, breadth chart, dashboard table | `apps/web/src/components/market/` |
| Market-surface deviations from `docs/01` §6-7 | `docs/11a-market-surfaces-notes.md` |
| `ScreenDefinition` (Zod mirror) | `packages/api-client/src/screen-definition.ts` |
| The trading calendar | `packages/core/src/decile_core/trading_calendar.py` |
| Reference CSV reader | `packages/core/src/decile_core/reference_export.py` |
| Seed rows (plans, screens, universes) | `packages/core/src/decile_core/seed_data.py` |
| Seed CLI (touches the DB) | `services/api/src/decile_api/seed.py` |
| Migrations | `services/api/alembic/versions/` |
| Provider ports and records | `packages/providers/src/decile_providers/{ports,records}.py` |
| Kite / NSE / composite / fixtures | `packages/providers/src/decile_providers/{kite,nse,composite,fixtures}.py` |
| Rate limiter, retry, circuit breaker | `packages/providers/src/decile_providers/{ratelimit,retry,circuit}.py` |
| Encrypted Kite token | `packages/providers/src/decile_providers/tokens.py` |
| Raw-file archive (docs/09) | `packages/providers/src/decile_providers/archive.py` |
| Provider fixtures + provenance | `tests/fixtures/providers/` |
| The suite-wide network block | `network_guard.py` |
| Celery app, queues, Beat schedule | `services/worker/src/decile_worker/celery_app.py` |
| The ten nightly steps | `services/worker/src/decile_worker/tasks/` |
| Pipeline orchestrator | `services/worker/src/decile_worker/orchestrator.py` |
| The 8 quality-gate assertions | `services/worker/src/decile_worker/tasks/quality.py` |
| Adjustment maths (pure) | `packages/core/src/decile_core/adjustments.py` |
| **The factor engine (pure)** | `packages/core/src/decile_core/factors.py` |
| Calendar-offset windows | `packages/core/src/decile_core/windows.py` |
| The 64-factor registry | `packages/core/src/decile_core/factor_registry.py` |
| Blend shapes + NULL rule | `packages/core/src/decile_core/blends.py` |
| Storage precision (round at write) | `packages/core/src/decile_core/precision.py` |
| INFERRED: circuits / skip-month | `packages/core/src/decile_core/{circuits,momentum}.py` |
| Wasserstein regime | `packages/core/src/decile_core/regime.py` |
| **Market-health breadth (pure query)** | `packages/core/src/decile_core/breadth.py` |
| PROS/CONS rules | `packages/core/src/decile_core/pros_cons.py` |
| Engine ↔ database wiring | `services/worker/src/decile_worker/engine.py` |
| `factors explain` CLI | `services/worker/src/decile_worker/factors_cli.py` |
| Trading-day guard + reconciliation | `services/worker/src/decile_worker/calendar.py` |
| Resumable bar backfill | `services/worker/src/decile_worker/backfill.py` |
| Reference-data backfill | `services/worker/src/decile_worker/reference_backfill.py` |
| PIT membership + source tracking | `services/worker/src/decile_worker/tasks/membership.py` |
| Index dashboard (~145) | `services/worker/src/decile_worker/tasks/snapshots.py` |
| Listings register | `services/worker/src/decile_worker/tasks/listings.py` |

## Commands

```
make up          start postgres + redis + mailpit, wait for health
make down        stop the stack
make migrate     alembic upgrade head
make downgrade   alembic downgrade base
make seed        reference data + trading days + the reference CSV fixture
make test        pytest + vitest
make test-db     only the tests needing a live database
make lint        ruff check + ruff format --check + mypy strict + tsc
make fmt         ruff format + ruff check --fix
make schema      regenerate the ScreenDefinition JSON Schema for packages/api-client
make doctor      report what each provider can currently serve
make mailpit     print the local inbox URL (everything the API emailed)
make fixtures    regenerate tests/fixtures/providers from the docs/13 export
make worker      run a Celery worker across all four queues
make beat        run Celery Beat (docs/09 §Schedule, IST)
make flower      inspect queues and tasks
make pipeline    run the nightly chain for one date: make pipeline DATE=2026-08-18
make backfill    resumable bar backfill: make backfill FROM=2011-01-01 TO=2026-08-18
make refdata     reference-data backfill: make refdata FROM=2018-01-01 TO=2026-08-18
make explain     audit one factor: make explain SYMBOL=CUPID DATE=2026-08-18 FACTOR=sharpe_12m
make openapi     emit packages/api-client/openapi.json from the FastAPI app
make client      regenerate the TypeScript client from openapi.json (CI fails if stale)
make web         run the Next.js dev server on :3000
make web-build   production build of the web app
make e2e         Playwright acceptance checks (builds and starts the app itself)
```

`make test-db` needs `DECILE_TEST_DATABASE_URL`. Without it those tests skip rather than fail.

## Open items carried forward

- **The GST rate and the SAC code need a chartered accountant.** 18% and SAC 998439 are defaults,
  not advice. Both are settings (`DECILE_GST_RATE_PERCENT`, `DECILE_GST_SAC_CODE`) and every
  issued invoice stores what it was raised at, so changing them cannot rewrite history — but
  nothing has confirmed them (`docs/DECISIONS.md` §13.2). **Confirm before the first real charge.**
- **The advertised prices are GST-inclusive.** `docs/01` §1 gives ₹500 / ₹3,999 / ₹14,999 with no
  mention of tax; charging ₹590 for a plan the page calls ₹500 would be the alternative. The
  taxable value is back-computed and the tax is the remainder, so the invoice total is always the
  payment to the paisa (`docs/DECISIONS.md` §13.1).
- **The Razorpay webhook has never seen a real delivery.** The signature scheme, the event names
  and the entity shapes are written from the documented formats, not against the live gateway —
  the suite is network-blocked and every test drives an `httpx.MockTransport`. Confirm against a
  test-mode delivery before taking money (`docs/DECISIONS.md` §13.17).
- **Nothing collects a customer GSTIN or a place of supply.** The columns, the arithmetic and the
  inter-state (IGST) path all exist and are tested; no UI fills them, so every invoice raised
  today is B2C, intra-state, at the supplier's own state. A B2B customer cannot claim input credit
  until a form exists.
- **`payment.status` never becomes `refunded`.** `refund.*` events are acknowledged and ignored.
  `docs/07` has no refund endpoint and `docs/11` names a Refund Policy page that is not written.
- **The rebalance tracker stores quantities and does nothing with them.** `quantity` and
  `avg_price` are imported, stored and echoed back; no weight, exposure or P&L is derived from
  them, because `docs/01` §8's tracker is a symbol diff. Target weights are equal-weight, which
  `docs/07` does not specify (`docs/DECISIONS.md` §14.1).
- **A BSE scrip code in an uploaded CSV is reported, never resolved.** We hold NSE instruments and
  have no BSE-code mapping; `532540` comes back `unmatched` with reason `bse_code`
  (`docs/DECISIONS.md` §14.4). The same goes for a symbol that names more than one listing — it
  is `ambiguous` with its candidates, and nothing is imported for it.
- **No holdings editor exists.** Holdings are set by CSV upload or by `PUT /portfolios/{id}/holdings`;
  there is no add-a-row form. `docs/08` §"Rebalance tracker" describes the wizard and asks for none.
- **The rebalance tracker has no Playwright coverage.** The rule, the parser, the endpoints and the
  React components are all tested, but no browser test walks the wizard end to end.
- **`docs/DECISIONS.md` §15 should become `docs/10a-backtest-implementation-notes.md`.** Same
  reason as §13 and §14: the overnight run could append to `DECISIONS.md` and change nothing else
  under `docs/`. Note that `docs/10a` is currently the *instrument factsheet* notes, so the
  backtest file needs a different number.
- **`dividends: "cash"` and `dividends: "ignore"` are refused, not implemented-and-wrong.**
  `docs/09`'s adjustment folds cash dividends into `adj_factor`, so `ohlcv_daily.close` is already
  a total-return series and crediting the dividend again would double count. `reinvest` is exact
  and is the default; the other two need a dividend-stripped price series nothing builds yet, and
  the engine raises rather than quietly serving `reinvest` (`docs/DECISIONS.md` §15.1).
- **The backtest's risk-free rate is a flat annual rate defaulting to 0.** `docs/10` §Outputs asks
  for "rf from a configurable T-bill series" and `docs/04` has no T-bill table. Every Sharpe and
  Sortino on the page is therefore an *excess-over-zero* figure until one exists
  (`docs/DECISIONS.md` §15.5).
- **The 15-year backtest budget is measured against a synthetic market, not the seeded dataset.**
  PROMPTS.md Prompt 15's last acceptance criterion says "on the seeded dataset"; the seeded
  database holds one trading day of *results* (`docs/13`'s export) and no price history, so no
  multi-year backtest can run against it at all. The budget is met with room to spare — 0.24 s for
  the pure engine over a 300-name panel, 1.44 s for the whole server-side path (181 screen queries
  + bar load + simulation) over a 250-name market seeded into PostgreSQL, against 10 s — but that
  is not the same measurement.
- **Until a real backfill has run, every backtest a user could queue fails.** `ohlcv_daily` holds
  no history, so the loader raises "no adjusted bars exist for any name this screen selected".
  That is correct behaviour and it will look like a bug in staging.
- **The API → broker → worker wire has never run.** `POST /backtests` publishes
  `decile.backtest.run` by name and the worker binds and routes it; both halves are tested
  separately, but no test starts a Celery worker. Same class of gap as the Razorpay webhook.
- **The backtest's `export` link is signed by us, not presigned by R2.** `docs/07` asks for a
  "signed URL"; the archive abstraction covers both a bucket and a directory, and a directory
  cannot presign. The link is an HMAC over `(public_id, artefact, expiry)` redeemed at an
  unauthenticated download route (`docs/DECISIONS.md` §15.15). CSV only — no Parquet writer.
- **Deleting a backtest leaves its R2 artefacts behind.** They are keyed by a `public_id` that is
  never reissued, so nothing can read them; but nothing sweeps them either
  (`docs/DECISIONS.md` §15.19).
- **The backtest surface has no Playwright coverage,** the same gap the rebalance tracker has.
- **An unpaid account's `max_screens` is 5, and 5 is invented.** `docs/07`'s example payload shows
  50, which is the paid number; the bundle gives no free-tier figure
  (`decile_core.entitlements.FREE_MAX_SCREENS`, `docs/DECISIONS.md` §13.12).
- **The ₹0 tier exists but is off.** `DECILE_FREE_TIER_ENABLED` gates it, and the plan row is only
  seeded while the flag is set — turning it on means re-running `make seed`. Its "limited
  universe" is NIFTY 50, which `docs/01` does not specify (`docs/DECISIONS.md` §13.14).
- **The invoice PDF is written by hand, in Courier.** `docs/02` locks no PDF library, so
  `decile_core.pdf` writes PDF 1.4 directly; Courier because its fixed 600/1000 em width makes
  right-aligned figures exact without transcribing a font-metrics table. A designed invoice is
  later, deliberate work (`docs/DECISIONS.md` §13.6).
- **`docs/DECISIONS.md` §13 should become `docs/13b-billing-implementation-notes.md`.** The
  overnight run was permitted to append to `DECISIONS.md` and change nothing else under `docs/`,
  so Prompt 13's implementation notes are collected there instead of in the numbered file the
  `07a`/`08a`/`12a` convention would put them in.
- **One intermittent test deadlock, seen once and not reproduced.**
  `test_seed.py::test_reseeding_does_not_demote_reconciled_days` failed with
  `DeadlockDetectedError` on its `DROP SCHEMA ... CASCADE` during a full `pytest` run, then passed
  on two further full runs and on every targeted run. Two connections to the same test database
  contending; worth pinning down before CI relies on it.

- **The trading calendar reconciles itself now, but only where data exists.** `reconcile_calendar`
  (Prompt 3) promotes dates with bars to `bhavcopy` and infers holidays across densely populated
  ranges, which is what fills in the lunar-calendar holidays the seed list is missing
  (`docs/04a-trading-day-addendum.md`). Until a full backfill has run, most of the calendar is
  still `derived` — a guess. Prompt 5 must still assert the 22/64/121/185/247 window lengths.
- **The 6 example screens are authored, not observed.** `docs/01` §1 records only that six exist.
  See the note in `seed_data.py`.
- **`docs/06`'s reference SQL skeleton is not PostgreSQL.** It uses `QUALIFY`, which only
  Snowflake and DuckDB have, and a bare `ORDER BY marketcap_cr DESC` that would put every
  NULL-marketcap row in the top decile. Both are translated, and every other departure from the
  letter of `docs/06` is written up in `docs/06a-screener-implementation-notes.md`. Read that
  before changing `screener.py`.
- **`docs/02` asks for credentials auth *and* Postgres sessions; Auth.js v5 cannot do both.** The
  Credentials provider requires `strategy: "jwt"`. The session is a cookie JWT; the Postgres
  adapter carries the user records and the OTP tokens (`docs/08a` §2).
- **The auth stub is gone.** Prompt 12 built `/auth/*`, so `apps/web` posts to the real endpoints,
  Argon2id verification happens where the hash lives, and the browser suite registers and signs in
  for real. `stub-endpoints.ts` and `DECILE_ALLOW_STUB_AUTH` were deleted (`docs/12a` §5).
- **Six auth tables are additions to `docs/04`,** which defines only `app_user.password_hash`.
  Each is required by a numbered line of `docs/11` §Security or §Compliance
  (`docs/04c-auth-tables-addendum.md`).
- **Codes and refresh tokens are stored as SHA-256; only passwords are Argon2id.** Neither has an
  offline guessing game to slow down, and a KDF on every token refresh is a DoS surface
  (`docs/12a` §3).
- **Refresh-token reuse revokes the whole family,** so a client that races itself can be signed
  out. That is the documented trade against a stolen cookie working indefinitely (`docs/12a` §4).
- **`ConsoleTransport` logs the OTP on purpose** and is refused in production by
  `require_configured`. Every other path is stripped by `RedactingFilter` (`docs/12a` §7).
- **`PATCH /me` cannot change an email address.** That needs the new address proved first, which
  is a separate flow and is not built (`docs/12a` §8).
- **The registration consent checkbox names Terms and a Privacy Policy that are not written.**
  `consent_record.document_version` is stored, so the published text will need fresh consent
  (`docs/12a` §10).
- **Erasure anonymises an account that has invoices and deletes one that does not.**
  `payment.user_id` is NOT NULL and GST retention outranks DPDP erasure (`docs/12a` §9).
- **Test addresses are `@example.com`, not `@decile.test`** — `email-validator` refuses the
  special-use `.test` TLD outright (`docs/12a` §12).
- **Volatility's ×100 happens in `apps/web/src/lib/format.ts` and nowhere else.** The screener and
  the API both store and serve the decimal fraction (`docs/06a` §10, `docs/07a` §13).
- **`apps/web` has zero `any`, enforced by a source scan** (`src/lib/__tests__/no-any.test.ts`),
  not only by an ESLint rule that an inline comment could switch off.
- **Nav items for unbuilt routes render disabled, with the prompt that delivers them.** Keep
  `apps/web/src/lib/nav.ts` honest when a route lands (`docs/08a` §10).
- **An out-of-range `as_of` is a 422, not a clamp.** Prompt 7 changed this: a weekend still snaps
  backwards (`docs/06` §step 1), but a date later than the latest published day or earlier than
  `DATA_START_DATE` (1 Nov 2024, `docs/01` §2.13) is refused as `no-trading-day`.
  `docs/06a` §11 records the supersession; `docs/07a` §1 has the table.
- **The CSV export is `docs/13`'s 93 columns, not the screen's column set.** Prompt 7 built the
  latter; `docs/13` §5 step 6 makes reproducing the committed fixture the acceptance test, and
  Prompt 9 §6 says the same. Line endings are `\n` and `name` is quoted on every row
  (`docs/09a` §1).
- **The screen-cache key hashes the definition *and* the projection.** `docs/06`'s formula covers
  only the definition, which serves the wrong columns after a column-layout save
  (`docs/06a` §8a, `docs/09a` §3).
- **`apps/web/src/lib/screens/operands.ts` duplicates `CUSTOM_FILTER_OPERANDS`,** because no
  `/meta/` endpoint publishes it. `packages/core/tests/test_operand_parity.py` is the link — keep
  the two in step. `apps/web/src/lib/market/universes.ts` duplicates the twelve market-health
  universes for the same reason, with `test_market_health_universe_parity.py` as its link.
- **The browser suite needs `make up` and its own database.** `playwright.config.ts` migrates and
  seeds `decile_e2e` and starts both servers; since Prompt 12 it also turns the Argon2id cost down
  and points email at mailpit, and signs in with `decile_api.seed.E2E_PASSWORD` (`docs/09a` §5,
  `docs/12a` §12).
- **Eight PROS fire for CUPID where `docs/01` §5 observed six.** `docs/05` §16 tables eight rules
  and says "ship at least" them; the six the reference product showed are the first six, asserted
  word for word on both sides (`docs/10a` §1).
- **A PROS/CONS rule with a NULL input is neither.** Rendering the negation would put a CON on a
  young listing for a fact nobody knows; `undecided()` names those rules instead (`docs/10a` §2).
- **The percentile bars compare against the *narrowest* universe the instrument is in that day,**
  because `docs/08` never defines "the current universe". It is named in the payload and on the
  page (`docs/10a` §4).
- **The two Wasserstein distances are recomputed, not stored** — `docs/04` has no column for them
  and `docs/05` §15 requires them exposed. The stored `regime` label still wins when populated
  (`docs/10a` §3).
- **`factor_daily.pe` is NULL everywhere,** so the factsheet's P/E stat and its "Price to Earnings"
  card render as em dashes. Same root cause as the fundamentals open item below (`docs/10a` §9).
- **Nothing calls `POST /api/revalidate` yet.** The instrument pages carry the `factsheet` cache
  tag and the route that purges it exists, but wiring the nightly publish step to make the request
  is not done — until it is, ISR refreshes on its one-hour timer (`docs/10a` §11).
- **The dashboard fixture carries 117 indices, not `docs/01` §7's ~145.** The last ~28 are names
  we would be guessing at, and a fixture of invented index names teaches the reader a market that
  does not exist. The row count is asserted (`docs/11a` §1).
- **The dashboard has no sector grouping, because `docs/04` has no sector column.** Deriving one
  by pattern-matching index names would be inventing a taxonomy and attributing it to NSE
  (`docs/11a` §2).
- **The breadth arithmetic is `decile_core.breadth`, executed by both the worker and the seed.**
  A second copy in the seed would make the hand-computed acceptance test meaningless
  (`docs/11a` §7).
- **`1Y Return > 0%` reads 100% on the seeded data** — `docs/13`'s export is a momentum screen's
  output, so every row in it is positive by construction. Asserted, so nobody reads it as a bug
  (`docs/11a` §8).
- **Breadth exists for one date only** until the pipeline has run over a real backfill, so the
  history charts say "a line needs two" rather than drawing one (`docs/11a` §8).
- **"Revalidate on `data_version`" is a data-cache tag, not a static route.** Everything under
  `(app)` renders dynamically because the shell reads cookies; the tagged fetch is what
  `revalidateTag` invalidates (`docs/11a` §6, correcting `docs/10a` §11).
- **`docs/07` does not say where the client sends `data_version`,** but its catalogue requires a
  409 for a stale one. It is an optional field on the run and preview bodies (`docs/07a` §2).
- **`api_access` is false for everyone and `X-API-Key` is ignored** until Prompt 20 has a key
  store. Prompt 7's "entitled to everything except the three" reads otherwise; `docs/07`'s own
  example payload does not (`docs/07a` §4).
- **`503 pipeline-degraded` means "no published version at all".** A *failed* run keeps serving the
  last good `data_version` with `degraded: true` on `/meta/status`, which is what `docs/11`
  §Reliability asks for (`docs/07a` §8).
- **`ignore_top_beta.count` / `ignore_top_volatility.count` are accepted and ignored.** `docs/06`
  settles the semantics as a boolean flag precomputed per universe at `TOP_RISK_FLAG_PERCENTILE`;
  a per-request count cannot be served by testing a bit (`docs/06a` §5).
- **The export's row order is reproducible only to ±0.0075.** `docs/13` §2 finding 3 says so
  itself: the reference product ranked on unrounded values the export does not carry. 114 of the
  271 positions differ from the file, every one of them inside that rounding granularity
  (`docs/06a` §7). Note that the 48 inversions `docs/13` reports *do* reproduce exactly with
  `Decimal` — the note below about 53 was a float artefact.
- **`ix_factor_daily_date_marketcap_cr` is dead weight today.** `ix_factor_daily_date` is a
  cheaper prefix index for the same predicate and PostgreSQL always picks it; neither can go
  index-only because a screen needs `instrument_id`. Prompt 16 should drop one or rebuild the
  other with `INCLUDE (instrument_id)` (`docs/06a` §8).
- **`docs/05` §2 and `docs/13` §4 disagree on volatility units** — percent vs decimal fraction.
  The export is the arbiter: it is a fraction. Resolve in Prompt 5.
- **`docs/02` and `docs/09` disagree on Kite adjustment.** `docs/02` says Kite gives
  "adjusted/unadjusted daily candles"; `docs/09` says "Kite returns unadjusted OHLC by default.
  Treat everything from Kite as raw." We follow `docs/09`: providers never adjust, and
  `apply_adjustments` (Prompt 3) derives `close` from `close_raw`.
- **Provider bar fixtures are synthetic.** Real symbols, names, index memberships, closing prices
  and CUPID's documented corporate actions; every bar before 2026-08-18 is a seeded random walk.
  See `tests/fixtures/providers/PROVENANCE.md`.
- **NSE URL shapes and column names are still unverified.** Written from the documented file
  layout, not against a live endpoint (the suite has no network). Confirm each URL and header row
  against a real fetch before the first production backfill.
- **THE DECISIVE PARITY TEST HAS NEVER RUN GREEN.** `docs/13` §5 step 2 reproduces every numeric
  column of all 271 export rows, which needs each instrument's adjusted daily closes for 248
  trading days. The bundle has no price history — `fixtures/` is one single-date snapshot of
  *results* — and the suite is network-blocked. `test_reference_parity.py` implements it in full
  and **skips**, with the reason attached. Set `DECILE_PARITY_BARS` to a Parquet file of real
  adjusted history and it runs. Everything the export *can* verify is verified and passing.
- **One figure in `docs/13` §2 does not reproduce from the committed file.** Max sharpe error is
  0.01, not 0.0051. (The blend-inversion count *does* reproduce as 48 when the blend is summed in
  `Decimal`; the earlier reading of 53 came from float arithmetic.) An artefact of re-deriving
  from already-rounded inputs, which does not overturn a formula — the error distribution has *nothing*
  between 0.00 and 0.01. Written up in `docs/13a-verification-discrepancies.md`.
- **`docs/01` miscounts twice.** §3 is headed "62 ranking factors" but enumerates 64; §4 says
  "34 available columns" but enumerates 36. We implement every named key.
- **Window lengths do not yet match `docs/13` §3.** The engine's window arithmetic is right (the
  1-month window resolves to 22 exactly), but the seeded calendar is short ~9 lunar-calendar
  holidays a year, giving 22/67/127/191/256 against the required 22/64/121/185/247. Fixed by
  running `reconcile_calendar` over a real backfill, not by changing the engine.
- **`compute_market_health` reports NULL, not 0%, until factors are populated for a date.**
- **Quality-gate assertion 6 reports SKIPPED, not PASSED.** It needs free-float weights and the
  index divisor to reconstruct a NIFTY 50 level; NSE publishes neither in the files `docs/09`
  lists and nothing populates `index_member_daily.weight`.
- **Assertions 3 and 7 are constraint checks, not data checks.** The PK and NOT NULL already
  forbid what they assert; they exist to catch a migration that relaxes either.
- **Rights issues are not adjusted.** TERP needs the subscription price, which NSE's free-text
  purpose usually omits. `decile_core.adjustments` returns `INSUFFICIENT_DATA` and leaves the
  series alone rather than guessing; every such action is surfaced in the step payload.
- **The ~145 dashboard indices are registered from data, not from a list.** The bundle names two
  of them (`docs/01` §7), so `refresh_index_snapshots` registers any index NSE publishes that we
  have no `index_def` row for, allocating ids from 100 upward. The 14 selectable universes stay
  hand-pinned at ids 1–14 because they carry `factor_daily` mask bits.
- **Pre-2018 index membership is reconstructed, and marked as such.** `index_member_daily.source`
  distinguishes `nse_file` from `reconstructed` and `derived` (`docs/09` §Backfill,
  `docs/04b`). Prompt 15's backtests **state** it rather than excluding it: every backtest's
  assumptions panel says so in as many words (`decile_api.backtests.assumptions`), which is what
  `docs/10` §"honesty features" asks for. Nothing filters a run by `index_member_daily.source`.
- **No pipeline step fetches fundamentals.** `docs/03`'s ten steps have no source for
  `fundamental_daily`, yet `factor_daily.marketcap_cr` and `.pe` are needed by the screener
  (`docs/06` buckets deciles by marketcap). Needs a decision in Prompt 4 or 5.
