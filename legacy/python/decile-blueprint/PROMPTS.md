# PROMPTS.md — the build sequence

22 prompts (numbered 0–21). Run them **in order**. One prompt per Claude Code session (start each with
`/clear`), so context stays clean and each module is independently reviewable.

## Before you start

1. Create an empty git repo and copy this whole bundle into `docs/` at the repo root.
2. Add a `CLAUDE.md` at the repo root — **Prompt 0 generates it.**
3. After every prompt: run the test suite, review the diff, commit on a branch, merge.
4. If a prompt's acceptance criteria fail, do **not** move on. Fix in the same session.

## House rules to repeat in every session

> These are already baked into `CLAUDE.md` after Prompt 0, but restate them if a session drifts:
> - Read `docs/02-tech-stack-adr.md` before proposing any dependency. Do not add libraries
>   outside the locked stack without saying why in the PR description.
> - Never write a test that asserts the current (possibly wrong) output. Tests assert the spec.
> - No `# type: ignore`, no `any`, no silently swallowed exceptions.
> - Every module ends with: tests passing, `make lint` clean, and a `docs/` update if behaviour
>   diverged from the spec.

---

# Prompt 0 — Repository, toolchain, CLAUDE.md

```text
You are setting up a new production monorepo for **Decile**, an India-equities momentum screener.
Read docs/14-brand-and-naming.md for the naming conventions to use throughout: repo root `decile`,
Python namespaces decile_core / decile_api / decile_worker / decile_providers, TS packages
@decile/web and @decile/api-client, database `decile`, env prefix `DECILE_`, images
ghcr.io/<org>/decile-{api,web,worker}.

Read docs/02-tech-stack-adr.md and docs/03-architecture.md in full first. They are the locked
technology decisions; do not deviate.

Create the repository skeleton exactly as described in the ADR's "Repo layout" section:
apps/web, services/api, services/worker, packages/core, packages/providers,
packages/api-client, infra/docker, .github/workflows.

Deliver:
1. pnpm workspace + Turborepo config for the TS side; uv (or Poetry — pick uv) workspace for the
   Python side with a shared pyproject and per-package pyproject files.
2. Python tooling: ruff (lint + format), mypy in strict mode, pytest with coverage, pre-commit.
3. TS tooling: ESLint flat config, Prettier, TypeScript strict, Vitest.
4. infra/docker/compose.yml with: postgres:16 + timescaledb extension, redis:7, mailpit,
   minio (S3-compatible, for local R2), and healthchecks. Include an init SQL that enables
   the timescaledb and citext extensions.
5. A Makefile with: make up, make down, make migrate, make seed, make test, make lint,
   make fmt, make api, make web, make worker.
6. .env.example covering every variable the ADR implies (DATABASE_URL, REDIS_URL, KITE_API_KEY,
   KITE_API_SECRET, S3_*, RAZORPAY_*, RESEND_API_KEY, JWT_SECRET, NEXT_PUBLIC_API_URL).
   Never commit real secrets.
7. .github/workflows/ci.yml: matrix job running Python lint+types+tests and TS lint+types+tests,
   plus a job that boots compose and runs a smoke test.
8. A root CLAUDE.md that captures: the locked stack, the repo layout, the "house rules" from
   docs, the commands above, and a short "where things live" map. Keep it under 120 lines.

Acceptance criteria:
- `make up && make test && make lint` succeeds on a clean clone.
- `docker compose ps` shows all services healthy.
- No application code yet beyond a health endpoint in services/api and a placeholder page in
  apps/web.

Do not scaffold any product features in this prompt.
```

---

# Prompt 1 — Database schema and migrations

```text
Read docs/04-data-model.md in full. Implement the complete schema.

Deliver:
1. SQLAlchemy 2.0 declarative models in packages/core/models/ for every table in the doc,
   with correct types (numeric not float for money/prices), constraints, and indexes.
2. Alembic set up in services/api/alembic with a single initial migration that creates
   everything, including: the TimescaleDB hypertables (ohlcv_daily, factor_daily,
   index_member_daily), the compression policy, and the citext extension usage on app_user.email.
3. A `trading_day` table plus a loader that seeds NSE trading holidays for 2011..current year.
4. Seed data migration/CLI for: exchange (NSE), index_def (the 14 selectable universes from
   docs/01 §2.1 plus the 12 market-health universes), plan (monthly/yearly/forever with the
   reference prices), and the 6 example screens from docs/01 §1 with valid definitions.
5. Pydantic v2 models for ScreenDefinition exactly matching the JSON shape in docs/04, with
   extra="forbid", field validators for the sentinel values (100 = ignore for away-from-high and
   ignore_above_beta, 0 = ignore for positive days, >250 = ignore for circuits), and a
   canonical_json() method that produces a stable hash input.
5b. Copy fixtures/reference-screen-export-2026-08-18.csv into the repo test fixtures and add a
   loader that can seed instrument, factor_daily and index membership rows from it, so later
   modules can test against real reference data without any network access.
6. A Zod schema in packages/api-client mirroring ScreenDefinition, generated or hand-kept in
   sync with a test that fails if they diverge.

Acceptance criteria:
- `make migrate` on an empty database succeeds; `alembic downgrade base` then `upgrade head`
  round-trips cleanly.
- A pytest asserts every table in docs/04 exists with the documented primary key.
- A pytest asserts ScreenDefinition rejects unknown keys and accepts the example screens.
- No table uses float for a price, quantity, or money column.
```

---

# Prompt 2 — Provider layer (Kite + NSE + fixtures)

```text
Read docs/09-data-pipeline.md, sections "Provider ports", "Kite specifics", "NSE specifics".

Implement packages/providers with the Protocol-based port design in that doc.

Deliver:
1. BarsProvider and ReferenceProvider Protocols, plus the record dataclasses/Pydantic models
   they exchange (InstrumentRecord, IndexSnapshot, CorporateAction, ListingRecord).
2. KiteProvider using the official kiteconnect client:
   - session/access-token handling with the token stored encrypted (Fernet) and a clear,
     loud error type when it has expired;
   - a Redis-backed token-bucket rate limiter (default 3 req/s) shared across processes;
   - daily_bars() that chunks requests to respect the day-interval cap, retries with
     exponential backoff + jitter on 5xx/429, and returns a Polars DataFrame with a fixed schema.
3. NSEProvider for: index constituents, index snapshots (level/PE/PB/div yield), corporate
   actions, listings, and bhavcopy (including series and circuit bands). It must:
   - prime cookies like a browser session,
   - archive every raw file to S3/R2 under nse/{kind}/{date}.{ext} BEFORE parsing,
   - parse from the archived copy, so parsing is reproducible without refetching.
4. CompositeProvider that routes by capability and wraps each provider in a circuit breaker.
5. FixtureProvider reading Parquet fixtures from tests/fixtures/, used by all tests and by
   `make seed` for local development. Include real-shaped fixtures for ~40 instruments over
   3 years, including at least one instrument with a split and one with a bonus.
6. A `providers doctor` CLI command that checks credentials and prints what each provider can
   currently serve.

Acceptance criteria:
- Zero network calls in the test suite (assert this with a socket-blocking fixture).
- Rate limiter test proves >3 req/s is impossible across two concurrent workers.
- A test proves KiteProvider retries and eventually raises a typed error after N attempts.
- `providers doctor` runs without credentials and reports each provider as unavailable rather
  than crashing.
```

---

# Prompt 3 — Ingestion pipeline and scheduler

```text
Read docs/09-data-pipeline.md in full.

Implement services/worker: Celery app, Beat schedule, and the ten pipeline tasks named in
docs/03 "Nightly pipeline".

Deliver:
1. Celery configured with Redis broker/result backend, task-level retries, and separate queues:
   `ingest`, `compute`, `backtest`, `default`.
2. Each of the ten tasks as an idempotent unit that upserts (never blind-inserts) and writes a
   pipeline_run_step row with rows_in/rows_out/duration/error.
3. A PipelineRun orchestrator that runs the chain for a trade date, stops on the first hard
   failure, and only bumps `data_version` after the quality gate passes.
4. The corporate-action adjustment algorithm exactly as specified in docs/09 "Adjustment
   algorithm", including the reprocess_instrument(instrument_id) task that rebuilds an
   instrument's whole adjusted history and its factor rows when a new action arrives.
5. The data-quality gate implementing all 8 assertions in docs/09, each as a named, individually
   testable check returning a structured result.
6. A resumable backfill CLI: `python -m worker.backfill --from --to --instruments --concurrency
   --resume`, with a cursor table so an interrupted run continues where it stopped.
7. Trading-day awareness everywhere: never attempt to ingest or compute for a non-trading day.

Acceptance criteria:
- Running the full pipeline twice for the same date produces zero row changes on the second run
  (assert with a checksum of the affected tables).
- A test injects a malformed day and proves the gate fails, data_version does NOT advance, and
  the previous version is still served.
- A test with a synthetic 2:1 split proves adjusted closes are continuous across the ex-date and
  that reprocess_instrument is idempotent.
- Backfill can be killed mid-run and resumed to the same final state.
```

---

# Prompt 4 — Reference data: universes, indices, listings, market health

```text
Read docs/04-data-model.md (index_def, index_member_daily, index_snapshot_daily,
market_health_daily) and docs/01-product-teardown.md §6 and §7.

Deliver:
1. refresh_index_membership: builds point-in-time index_member_daily for all 14 selectable
   universes plus the 12 market-health universes. `nifty-allcap` = all EQ instruments with a bar
   that day; `etf` = all ETF instruments. Record a `source` column value of 'nse_file' or
   'reconstructed' so backtests can flag uncertain history.
2. refresh_index_snapshots: ~145 indices with level, absolute change, % change, PE, PB, div yield
   into index_snapshot_daily. Handle indices that publish no fundamentals (store NULL, render
   as '-').
3. compute_market_health: for each of the 12 universes, compute pct_above_200dma,
   pct_above_50dma, pct_within_10pct_ath, pct_ret_1y_positive, and constituent_count, from
   factor_daily. Store daily so history is queryable.
4. refresh_listings: NSE listing dates and series for the full universe (~3,500 rows), upserted.
5. A backfill mode for all of the above over the historical range.

Acceptance criteria:
- A test asserts that resolving the NIFTY 500 universe for a past date does NOT use today's
  membership (build a fixture where membership changed and assert the difference).
- Market-health percentages for a fixture universe are computed by hand in the test and matched
  exactly.
- Index snapshot rows exist for every trading day in the fixture range with no duplicates.
```

---

# Prompt 5 — Factor engine

```text
Read docs/05-factor-formulas.md IN FULL and follow it literally. This is the numerical contract
of the product; deviations here silently corrupt everything downstream.

Implement packages/core/factors.py as pure Polars expressions: DataFrames in, DataFrames out,
no database access, no I/O.

Deliver:
1. Calendar-offset windows per docs/05 "Notation" — start = as_of minus relativedelta(months=K),
   snapped forward to a trading day, then all trading days in the span. NOT fixed bar counts.
   Assert the recovered lengths 22/64/121/185/247 for as-of 2026-08-18 against the reference
   fixture. Instruments without a full window get NULL and are excluded.
2. Every factor in docs/05 §1–§15: returns, volatility (annualised per window), "sharpe return"
   (= ret/vol, NO risk-free rate), RSI (Wilder, period = window length), beta vs Nifty 50,
   beta-scaled factors, 12−1 and 12−2 momentum, MAs, highs and away-from-high, positive days,
   circuit-hit counts, volume aggregates and median rupee turnover, and the Wasserstein regime
   classifier (return the label AND both distances).
3. The blend helper: arithmetic mean of components, NULL if ANY component is NULL. Do not store
   blends — expose the list of blend shapes from docs/05 §4 as a registry.
4. The factor registry described in docs/06 "The factor registry": for each of the 62 sort
   factors, a key, label, family, SQL expression string, unit, higher_is_better, and null policy.
   This single registry must drive the sort_by enum, the custom-filter operand list, the column
   picker, and the backtest signal list.
5. compute_factors task that materialises factor_daily for a date (and for a date range in
   backfill), vectorised — no Python loops over instruments.
6. A `factors explain --symbol CUPID --date 2026-08-19 --factor sharpe_12m` CLI that prints the
   intermediate series and the final value, for auditability.
7. Storage precision exactly as docs/13 §4: round at WRITE time (prices/returns/sharpe/
   away-from-high/positive-days 2 dp, RSI 4 dp, volatility and beta 10 dp with volatility stored
   as a decimal FRACTION not a percentage, marketcap integer ₹ crore, volumes bigint rupees).
8. `vol_day_val` must use exchange traded turnover in ₹ where available, not close × shares
   (docs/05 §13) — record which source was used per row.
9. Materialise the denormalised universe / top-beta / top-volatility masks onto factor_daily
   (docs/04, docs/06 "Universe flags"), with TOP_RISK_FLAG_PERCENTILE = 0.10 as a named constant,
   computed over the WHOLE universe, and an assertion that the masks agree with
   index_member_daily.

Acceptance criteria — implement every golden test in docs/05 "Golden-test requirements":
- CUPID identities reproduce: ret_12m/vol_12m == sharpe_12m for 1y/6m/3m/1m to 2 dp, and
  mean(sharpe_12m, sharpe_6m, sharpe_3m, sharpe_1m) == 5.19 to 2 dp on the fixture.
- away_high_ath reproduces -4.83% on the CUPID fixture.
- Constant-return series → vol 0, sharpe NULL.
- Split-adjustment invariance: factors identical before and after adjustment on a synthetic split.
- Blend with one NULL component → NULL.
- Beta of the benchmark against itself == 1.0 exactly.
Additionally: a benchmark test asserting full-universe factor computation for one date completes
in under 30 seconds on the fixture-scaled dataset.

**THE DECISIVE ACCEPTANCE TEST.** Load fixtures/reference-screen-export-2026-08-18.csv (271 real
rows from the reference product, trade date 2026-08-18) and write tests/test_reference_parity.py
per docs/13 §5: every numeric column of every row must be reproduced within the tolerance implied
by its stored precision, and the identities in docs/13 §2 must hold in our own output. Treat any
column that cannot be reproduced as a specification bug to be investigated and documented, not as
a test to be loosened.

Flag clearly in code comments the two INFERRED areas (12−1 momentum definition, circuit
detection) and expose them as configurable strategies so they can be recalibrated without a
rewrite.
```

---

# Prompt 6 — Screener query engine

```text
Read docs/06-screener-semantics.md IN FULL. Implement the seven-step pipeline exactly, in order.

Deliver packages/core/screener.py + services/api integration:
1. A ScreenQueryBuilder that turns a validated ScreenDefinition + as_of date into ONE
   parameterised SQL statement following the reference skeleton in docs/06.
2. Correct handling of, in this order: as-of resolution (snap backwards to the previous trading
   day and report the date actually used), point-in-time universe resolution,
   apply_filters_on bucketing by marketcap with DECILE_RANK_KEY as a named constant,
   all absolute filters, then the two "ignore top N beta/volatility" relative filters as a
   windowed exclusion over the survivors, then per-factor ROW_NUMBER ranking with per-factor
   direction, then combined = r1+r2+r3 ordered ascending with r1 as tie-breaker.
2b. "Ignore Top Beta / Volatility" reads the PRECOMPUTED per-universe flags (docs/06 "Universe
   flags"), selecting the mask bit for the screen's current universe. Do NOT implement it as a
   post-filter windowed exclusion — the reference export proves the cut is taken over the whole
   universe. Include a test using the fixture that proves the flagged set is separable by a
   strict beta threshold within each universe.
3. Custom filters as field-vs-field comparisons using ONLY registry keys — assert that no user
   string can reach the SQL text. Operators limited to >=, <=, =.
4. NULL semantics: NULLs never satisfy a predicate; NULLS LAST in every ranking.
5. Redis caching keyed exactly as docs/06 "Caching" specifies, with namespace invalidation on
   data_version bump and a warm_cache task for the top 200 screen definitions.
6. Result projection honouring the screen's saved column list plus the default columns.

Acceptance criteria:
- A SQL-injection test attempts to pass a malicious factor key and custom-filter operand; both
  must be rejected before query construction.
- A test builds a fixture where the "ignore top 5 beta" filter would give a different answer if
  applied before other filters, and asserts our ordering.
- A test asserts a screen with a 1-year filter excludes an instrument listed 3 months ago.
- Determinism test: same definition + as_of + data_version → byte-identical JSON twice.
- Performance test: the generated SQL for the full universe returns in under 300 ms cold on the
  seeded dataset; assert with EXPLAIN that it uses the (date, marketcap_cr) index.
- Every one of the 62 factors is exercised by a parametrised test that runs a screen sorted by it
  and asserts a non-empty, correctly ordered result.
- Reproduce the reference fixture end to end: running the "Investing 001" definition (universe
  NIFTY TOTAL MARKET, sort AVERAGE SHARPE RETURN 12 6 3 1 MONTHS, as_of 2026-08-18) against a
  database seeded from the fixture returns the same 271 symbols in the same order.
- The index-construction identities in docs/06 "Universe flags" hold with zero violations.
```

---

# Prompt 7 — FastAPI service: metadata, screens, results

```text
Read docs/07-api-spec.md. Implement the API service for the metadata, screens, and run endpoints
(the rest come in later prompts).

Deliver:
1. FastAPI app with: settings via pydantic-settings, async SQLAlchemy session dependency,
   structured JSON logging with request ids, OTel instrumentation, and RFC 9457
   problem+json error handlers for the full error catalogue in docs/07.
2. Endpoints: /meta/factors, /meta/columns, /meta/universes, /meta/trading-days, /meta/status;
   full CRUD for /screens; /screens/{id}/run; /screens/preview; /screens/{id}/csv;
   /screens/{id}/runs; /screens/{id}/duplicate.
3. JWT verification middleware (HS256, shared secret with the web app) + an entitlement
   dependency that returns 402 problem+json with an upgrade_url when a gated feature is used.
   For now, everyone is entitled to everything except export/custom columns/historical ranks,
   which check a stub entitlement service (replaced in Prompt 13).
4. Every analytics response carries as_of and data_version.
5. Rate limiting via Redis (60/min authed, 10/min anon).
6. CSV export streamed, not buffered, with the screen's column set and a UTF-8 BOM for Excel.
7. openapi.json emitted at build time into packages/api-client, and a CI step that regenerates
   the TypeScript client and fails if the checked-in client is stale.

Acceptance criteria:
- Contract tests for every endpoint using httpx + a seeded test database.
- A test asserts /screens/{id}/run with a historical_date in the future returns 422 no-trading-day.
- A test asserts a stale data_version request returns 409.
- Generated TS client compiles and its types match a hand-written assertion fixture.
- p95 of /screens/{id}/run against the seeded dataset is under 150 ms warm (assert in a
  benchmark test, not a unit test).
```

---

# Prompt 8 — Web app shell and design system

```text
Read docs/08-ui-spec.md sections "Design principles", "App shell", "Accessibility".

Set up apps/web as a Next.js 15 App Router application.

Deliver:
1. Tailwind v4 + shadcn/ui initialised with a custom theme: neutral greyscale scale, ONE accent,
   semantic positive/negative tokens that pass 4.5:1 in both light and dark. Tabular numerals
   globally for numeric cells.
2. Layout: collapsible sidebar with the exact navigation groups in docs/08, top bar with global
   ⌘K instrument search (command palette, wired to /instruments?search=), a data-freshness pill
   reading /meta/status, theme toggle, user menu, and a dismissible announcement banner slot.
3. Core primitives, each with stories or a /kitchen-sink route: DataTable (TanStack Table +
   Virtual, sticky header, optional repeating header every N rows, comfortable/compact density),
   StatCard, MetricGrid, Sparkline, FilterAccordion (with active-count badge), SentinelNumberInput
   (renders "off" at its sentinel and explains it via aria-describedby), FactorCombobox
   (searchable, grouped by family), Disclaimer, EmptyState, ErrorState.
4. TanStack Query provider, the generated API client wired with auth headers, and nuqs for
   URL-synced state.
5. Auth.js v5 configured with a credentials + email-OTP provider and a Postgres adapter; issue
   the HS256 JWT the API expects. Login/register/forgot-password pages can be placeholders that
   work end-to-end against stubbed endpoints for now.
6. next/font, metadata defaults, robots.txt, sitemap route, and a working dark mode with no FOUC.

Acceptance criteria:
- Lighthouse ≥ 95 accessibility on / and /kitchen-sink in both themes.
- DataTable renders 4,000 rows at 60fps while scrolling (measure with a Playwright trace).
- Keyboard-only walkthrough of the shell is possible: skip link, focus rings, no traps.
- No layout shift on theme toggle or on data load (skeletons reserve space).
- Zero `any` in apps/web.
```

---

# Prompt 9 — Screens UI: list, editor, results, columns, export

```text
Read docs/08-ui-spec.md "Screen editor" and "Columns editor", plus docs/01-product-teardown.md
§2 for the complete field inventory and its exact wording.

Deliver:
1. /screens — Example Screens and Your Screens sections, each card showing name, index, ranking
   factor, order (mirroring the reference product), plus Run / Duplicate / Delete.
2. /screens/[id] — the two-pane editor:
   - Left: the filter form with the accordion groups in the reference's order, every field from
     docs/01 §2.1–§2.14, with the reference's explanatory microcopy for sentinel values, active
     filter count badges per group, and validation messages from the API's problem+json errors.
   - Right: the results panel with the header strip (N results / Results are shown for <date> /
     Sorting Factor Column's Value = <FACTOR>), the virtualised table, client-side re-sort, row
     peek drawer, and the explanatory empty state described in docs/08.
3. Debounced live preview (400 ms) via /screens/preview, an explicit "Update & Apply Filters"
   that persists via PATCH, and a visible unsaved-changes state.
4. Full form state mirrored into the URL with nuqs so a screen configuration is shareable and
   back-button correct.
5. /screens/[id]/columns — the 34 toggles grouped by family, drag-to-reorder, live header
   preview, save.
6. Export button hitting /screens/{id}/csv, gated by entitlement with an upgrade prompt on 402.
   The CSV must match the reference export byte-for-byte in shape: the 93 columns in the order
   listed in docs/13 §1, UTF-8 **with BOM**, only the `name` field quoted. Add a test that diffs
   our export's header against fixtures/reference-screen-export-2026-08-18.csv.
7. Multi-factor UI: Factor Two / Factor Three toggles each revealing a FactorCombobox +
   direction, with an inline explanation of the combined-rank algorithm copied in substance from
   docs/06 (rank by each factor, sum the ranks, ascending sort).
8. Custom Filters UI: three slots, each `left field` `>=|<=|=` `right field`, both operands from
   the registry list in docs/01 §2.14.
9. Historical Ranks: date picker restricted to trading days from /meta/trading-days, with a clear
   banner when viewing a historical date.

Acceptance criteria:
- Playwright e2e: create a screen, set index + factor + three filters, apply, assert the result
  count and the first symbol against a seeded expectation, edit columns, export CSV, delete.
- Playwright e2e: toggling a sentinel field to its "ignore" value visibly marks it inactive and
  removes it from the group's active count.
- A test asserts URL round-trip: encode a full filter state, reload, and the form matches.
- No filter in docs/01 §2 is missing from the UI (write a test that reads the ScreenDefinition
  schema and asserts a form control exists for every field).
```

---

# Prompt 10 — Instrument factsheet

```text
Read docs/01-product-teardown.md §5 for the exact block order and docs/08-ui-spec.md
"Instrument factsheet". Read docs/05 §16 for the PROS/CONS rule table.

Deliver:
1. API: GET /instruments/{symbol}, /history, /corporate-actions, /rank-history, and the search
   endpoint, all per docs/07.
2. The PROS/CONS rule engine in packages/core as a single table of (predicate, pro_text,
   con_text), shared by API and UI. Ship at least the eight rules in docs/05 §16.
3. Own-history medians for the metric cards (closing price, rolling 1-year return, P/E,
   marketcap, 1-year RSI) computed over the instrument's available history.
4. /instruments/[symbol] page rendering every block in the reference's order: header with index
   membership chips, key stats, PROS/CONS, metric cards with sparklines and medians, price and
   moving averages, returns, sharpe returns, volatility, RSI, market quality (regime + median
   volume + circuits + positive days), corporate actions table.
5. Our improvement over the reference: each returns/sharpe/vol/RSI cell shows a subtle
   percentile bar within the instrument's current primary universe. Include a tooltip explaining
   the comparison set.
6. SEO: server-rendered per-instrument title/description, JSON-LD, per-instrument OG image,
   ISR revalidated on data_version.

Acceptance criteria:
- A test asserts the rendered PROS list for the CUPID fixture matches the reference product's
  six lines exactly in substance.
- A test asserts away-from-high and the returns block match the values in docs/05 to 2 dp.
- The page renders correctly for an instrument with < 1 year of history (NULLs shown as '—',
  never as 0).
- Lighthouse SEO ≥ 95 on the instrument route.
```

---

# Prompt 11 — Dashboard, Market Health, Listings

```text
Read docs/01-product-teardown.md §6 and §7, and docs/08-ui-spec.md "Dashboard", "Market Health".

Deliver:
1. GET /indices/dashboard, GET /market-health, GET /market-health/history, GET /listings.
2. /dashboard — all ~145 indices with % change, level, absolute change, PE, PB, div yield;
   sortable by any column; search; card/table view toggle; a 30-day sparkline per index; '-'
   rendering for indices without fundamentals. Sorted by % change desc by default.
3. /market-health — universe selector (the 12 universes), the four breadth gauges, the
   "Data available from <date>" note driven by the actual earliest row, PLUS our addition: a
   history chart per breadth series with the universe's index level overlaid, with range presets.
4. /listings — paginated (100/page, cursor-based) table of listing date / symbol / name / series,
   newest first, with a series filter and search.

Acceptance criteria:
- Breadth percentages on the seeded dataset are verified against a hand-computed fixture.
- /dashboard renders 145 rows with no virtualisation jank and sorts client-side without refetch.
- /listings pagination is stable across pages (no duplicates, no skips) under a cursor test.
```

---

# Prompt 12 — Auth, accounts, profile

```text
Read docs/07-api-spec.md "Account & billing" (auth portion) and docs/11-nonfunctional.md
"Security".

Deliver:
1. API: register, login, refresh, logout, request-otp, verify-otp, forgot-password,
   reset-password, GET/PATCH /me, change-password.
2. Argon2id hashing, email OTP as the primary path with password optional, rotating refresh
   tokens in httpOnly SameSite=Lax Secure cookies, CSRF protection on cookie-auth mutations,
   per-IP and per-account rate limiting with lockout after 10 failures plus a notification email.
3. Transactional email via Resend with local delivery to mailpit: verification, OTP, password
   reset, lockout notice. Templates in one place, plain-text alternatives included.
4. Web: login, register, forgot/reset password, /profile, /change-password, and session handling
   with correct redirects for gated routes.
5. Account deletion and data export endpoints (DPDP compliance), with a 7-day soft-delete window.

Acceptance criteria:
- Security tests: brute-force triggers lockout; refresh-token reuse is detected and revokes the
  family; a token signed with the wrong secret is rejected.
- A test asserts no password or token value is ever written to logs.
- Playwright e2e covering register → verify → login → change password → delete account.
```

---

# Prompt 13 — Plans, checkout, invoices, entitlements

```text
Read docs/07-api-spec.md "Account & billing" and "Entitlements", plus docs/11-nonfunctional.md
"Compliance & legal".

Deliver:
1. Plan catalogue seeded with Monthly ₹500/mo, Yearly ₹3,999/yr, Forever ₹14,999 one-time, with
   the feature sets from the reference product (screener, export, custom columns, historical
   ranks, backtests, community access, AMAs) expressed as an entitlements JSON.
2. Razorpay integration: subscriptions for monthly/yearly, one-time order for forever;
   /checkout/session; a signature-verified, idempotent /webhooks/razorpay that is safe to replay;
   subscription lifecycle handling (created, charged, halted, cancelled, expired).
3. A single EntitlementService used by BOTH the API dependency and the web UI (via /me), so the
   UI only reflects server truth. Replace the stub from Prompt 7.
4. Invoices: sequential invoice numbers, GST fields (GSTIN, HSN/SAC, place of supply), PDF
   generation stored in R2, /invoices list and download.
5. /pricing page mirroring the reference's structure including the "Forever means the lifetime of
   the website" clarification and the pre-purchase disclaimer bullets. A ₹0 free tier with a
   limited universe is optional — implement it behind a feature flag.
6. Gating applied to: CSV export, custom columns, historical ranks, backtests, and a max_screens
   limit.

Acceptance criteria:
- Webhook replay test: the same event delivered five times produces exactly one payment row.
- A test asserts a downgraded/expired user loses gated endpoints immediately (402 with
  upgrade_url) while retaining read access to their saved screens.
- Invoice numbers are gapless and unique under concurrent payment creation.
- No price or entitlement is hard-coded in the web app; all read from the API.
```

---

# Prompt 14 — Portfolios and rebalance tracker

```text
Read docs/01-product-teardown.md §8 and docs/07-api-spec.md "Portfolios & rebalance".

Deliver:
1. Portfolio CRUD with holdings, plus CSV import that returns a parse report listing matched,
   ambiguous and unmatched symbols rather than silently dropping rows. Provide a downloadable
   sample CSV, as the reference product does.
2. POST /portfolios/{id}/rebalance implementing the rank-buffer rule:
   - entries: in the screen's top_n, not currently held
   - exits: held and ranked worse than top_n + hold_buffer (or absent from the screen entirely)
   - inside_wrh: held, rank > top_n but <= top_n + hold_buffer → hold
   Return target weights alongside the three lists.
3. Web wizard: pick portfolio (or upload) → pick screen → set top_n and hold_buffer → three
   result columns each with copy-to-clipboard and CSV download, matching the reference's
   Exits / Inside WRH / Entries layout, plus an explanation of the buffer rule.
4. Rebalance history: persist each computed rebalance so a user can see what they were told and
   when.

Acceptance criteria:
- Unit tests for the buffer rule covering: held name at rank exactly top_n+hold_buffer (hold),
  at +1 beyond (exit), a delisted holding (exit with reason), and a symbol not in our universe.
- CSV import test with messy input: extra columns, whitespace, lowercase symbols, a BSE-style
  code, and a blank row.
```

---

# Prompt 15 — Backtest engine

```text
Read docs/10-backtest-spec.md IN FULL. This module must be correct before it is fast.

Deliver:
1. packages/core/backtest.py: a vectorised, point-in-time engine implementing the execution
   model in docs/10 exactly — screen as of the rebalance date, hold-buffer selection, weighting
   with position limits, execution at the NEXT trading day's open, costs on traded notional,
   whole-share rounding, daily mark-to-market, delisting liquidation, configurable dividend
   policy.
2. A hard look-ahead guard at the data-access layer: any read whose date exceeds the current
   as_of raises. This guard is always on in backtests, not a debug flag.
3. All metrics and artefacts listed in docs/10 "Outputs". Large artefacts to R2; metrics and a
   downsampled equity curve in Postgres.
4. Celery task on the `backtest` queue with progress events streamed to the client over SSE,
   a per-user concurrency cap of 1, and a global cap.
5. API: POST/GET/DELETE /backtests, /trades, /export per docs/07.
6. Web: config form, queue/progress UI, results page with equity curve vs benchmark, drawdown
   chart, metrics table, per-rebalance holdings, trade log, and the assumptions panel.
7. The "fragility" readout from docs/10: re-run with ±1 rebalance-day offset and ±25% costs and
   show the spread of outcomes.

Acceptance criteria — implement all six tests in docs/10 "Correctness harness":
look-ahead trap raises; buy-and-hold reproduces the benchmark within costs; zero-cost
zero-turnover equals the equal-weighted universe; determinism by metrics hash; survivorship
fixture shows the loss; cost monotonicity holds.
Plus: a 15-year monthly backtest over 20 positions completes in under 10 seconds on the seeded
dataset.
```

---

# Prompt 16 — Performance, caching, and load hardening

```text
Read docs/11-nonfunctional.md "Performance budgets" and docs/06 "Caching".

Deliver:
1. A benchmark suite (pytest-benchmark + k6 or Locust) covering: screen run cold/warm, dashboard,
   factsheet, CSV export, and the nightly factor computation.
2. Query tuning: EXPLAIN ANALYZE every hot query, add or adjust indexes, and add a CI check that
   fails if any hot query's plan changes to a sequential scan on the seeded dataset.
3. Redis caching per docs/06, plus HTTP caching: ETags derived from data_version, stale-while-
   revalidate on RSC fetches, and cache warming after publish.
4. Timescale compression policies verified, and continuous aggregates for market-health history
   and index history.
5. Frontend: route-level code splitting so the screens bundle stays under 250 KB gzip, dynamic
   import of charts, and a bundle-size CI budget.
6. Connection pooling (pgbouncer or asyncpg pool sizing) tuned and documented.

Acceptance criteria:
- Every budget in docs/11 is met by an automated benchmark, and the numbers are written into
  docs/11 as an "as measured" column.
- A load test at 50 concurrent screen runs sustains p95 < 400 ms with no error rate.
```

---

# Prompt 17 — Observability, admin, and operations

```text
Read docs/09 "Observability", docs/03 "Nightly pipeline", docs/11 "Reliability".

Deliver:
1. OpenTelemetry traces across web → API → worker → database, with trade_date, rows_in/out and
   screen definition hash as span attributes. Sentry for exceptions in all three runtimes.
2. Prometheus metrics + Grafana dashboards for: pipeline step durations, provider error rates,
   gate pass/fail, publish latency (EOD close → data live), API latency by route, cache hit rate,
   queue depth.
3. Alerting rules: pipeline failure, Kite token expiry, gate failure, publish later than
   20:15 IST, error rate > 1%, queue backlog.
4. /admin (staff-only): pipeline run history with per-step detail and a re-run button,
   data_version history, provider health, user lookup, entitlement override, and a
   reprocess-instrument action.
5. Runbooks in docs/runbooks/: kite-token-expired.md, pipeline-failed.md, bad-data-published.md
   (including how to roll data_version back), restore-from-backup.md, razorpay-webhook-replay.md.
6. Automated backups with a monthly restore-drill CI job that provisions a scratch database from
   the latest backup and runs a data-integrity assertion.

Acceptance criteria:
- Killing the worker mid-pipeline produces a correct failed run record and an alert.
- The restore drill job passes end to end.
- Every runbook has been executed once against staging and updated with real output.
```

---

# Prompt 18 — Content, marketing, and legal pages

```text
Read docs/01-product-teardown.md §1 for the content surfaces and docs/11 "Compliance & legal".

Deliver:
1. Marketing landing page: what the tool does, the factor families, a live sample screen preview,
   pricing summary, and the SEBI disclaimer. Distinctive, dense, and honest — not a generic SaaS
   template.
2. /pricing (from Prompt 13), /faq with the reference product's question set answered for our
   product, /about, /support with a contact form, /blog with MDX posts and RSS, and an
   announcement page pattern like the reference's "December 2026 update".
3. Legal: /terms-conditions, /privacy-policy, /refund-policy, /disclaimer. Draft them
   specifically for this product (Indian jurisdiction, non-SEBI-registered, prepaid lifetime
   plan treatment, data licensing constraints). Mark them clearly as DRAFTS REQUIRING LEGAL
   REVIEW at the top of each file in the repo, not on the rendered page.
4. SEO: sitemap including every instrument page, structured data, canonical URLs, OG images.
5. A cookie/consent banner that defaults to essential-only.

Acceptance criteria:
- All pages are statically generated and score ≥ 95 on Lighthouse performance and SEO.
- The Disclaimer component appears on every analytics route (assert with a Playwright sweep of
  the route table).
- No page claims or implies investment advice; add a copy-lint test with a banned-phrase list
  ("guaranteed returns", "buy now", "recommendation", "advice").
```

---

# Prompt 19 — Test hardening and data-correctness audit

```text
This module adds no features. It makes the system trustworthy.

Deliver:
1. Coverage: ≥ 90% on packages/core, ≥ 80% on services/api. Fail CI below those thresholds.
2. Property-based tests (hypothesis) for the factor engine: monotonicity (higher prices → higher
   returns), scale invariance (multiplying an entire price series by a constant leaves returns,
   volatility, RSI and beta unchanged), and NULL propagation through blends.
3. A cross-validation harness: pick 25 real instruments, compute every factor with an
   independent, deliberately naive pandas implementation, and assert agreement with the Polars
   implementation to 4 decimal places. Keep both implementations; the naive one is the oracle.
4. A reconciliation report CLI comparing our computed values against a snapshot of the reference
   product's public output for a set of symbols — starting with the 271-row reference export in
   fixtures/ — printing every disagreement over a tolerance.
   Investigate and document each disagreement — especially the two INFERRED areas (12−1
   momentum, circuit detection) — and update docs/05 with the resolved definitions.
5. Playwright e2e suite covering the ten critical user journeys end to end, run against a seeded
   database in CI.
6. Mutation testing (mutmut or cosmic-ray) on packages/core/factors.py and screener.py; fix or
   justify every surviving mutant.

Acceptance criteria:
- The reconciliation report runs clean or every deviation has a written, committed explanation.
- CI is green, deterministic, and completes in under 15 minutes.
```

---

# Prompt 20 — Public API, alerts, and API keys

```text
These are our differentiators over the reference product. Read docs/07 and docs/11 "Compliance"
first — especially the data-licensing constraint.

Deliver:
1. API keys: creation, scoping (read-only), rotation, revocation, per-key rate limits, and a
   usage dashboard. Keys are hashed at rest and shown once.
2. A versioned public read API exposing ONLY derived analytics (screen results, factor values,
   breadth), never raw vendor bars, with a machine-readable terms-of-use endpoint. Gate the
   entire feature behind a flag that stays OFF until the data-redistribution review in docs/11
   is signed off; make that dependency explicit in the code and the admin UI.
3. Screen alerts: a user subscribes a screen to a schedule (daily/weekly after publish) and
   receives an email with entries, exits, and rank changes since the last run — computed by
   diffing screen_run rows. Include an unsubscribe link and a digest preference.
4. Webhooks for entries/exits on a screen, with HMAC signing and retry with backoff.
5. Docs: an interactive API reference (Scalar or Redoc) served from the OpenAPI spec, with
   copy-paste examples in curl, Python and TypeScript.

Acceptance criteria:
- A test asserts a revoked key is rejected within one second (no cached-auth window).
- Alert diffing test: given two consecutive screen_run fixtures, the email content matches an
  expected snapshot exactly.
- The public API flag defaults to OFF in every environment config in the repo.
```

---

# Prompt 21 — Deployment and launch

```text
Read docs/03 "Environments", docs/11 "Reliability", and the ops runbooks from Prompt 17.

Deliver:
1. Production Docker images for web, api and worker, multi-stage and non-root, built and pushed
   by CI to GHCR with SBOM and image scanning.
2. infra/docker/compose.prod.yml with Traefik (automatic TLS), all services, resource limits,
   log rotation, and healthcheck-gated startup ordering.
3. Zero-downtime deploy script: build → run migrations → roll the API → roll the web → roll
   workers, with an automatic rollback on healthcheck failure.
4. Staging environment mirroring production with test Razorpay keys and a nightly production-
   data refresh (with PII scrubbed).
5. A launch checklist committed as docs/launch-checklist.md, covering at minimum: backfill
   verified from 2011, gate green for 5 consecutive trading days, restore drill passed, legal
   pages reviewed, Razorpay live keys and webhook verified with a ₹1 test transaction, alerting
   paging correctly, disclaimer sweep passed, load test passed, DNS/TLS/HSTS confirmed,
   sitemap submitted, and a rollback plan for the first 48 hours.
6. A post-launch monitoring plan for week one: what to watch, thresholds, and who is on call.

Acceptance criteria:
- A full deploy to staging from a clean state succeeds via one command.
- A deliberately failing healthcheck triggers an automatic rollback in staging.
- Every item on the launch checklist is either ticked or has an owner and a date.
```

---

## Suggested sequencing and effort

| Phase | Prompts | Outcome | Rough effort |
|---|---|---|---|
| Foundation | 0–2 | repo, schema, providers | 1 week |
| Data | 3–5 | real data flowing, factors verified | 2 weeks |
| Engine + API | 6–7 | screens run correctly and fast | 1.5 weeks |
| Product UI | 8–11 | the app is usable end to end | 3 weeks |
| Business | 12–13 | accounts and revenue | 1.5 weeks |
| Depth | 14–15 | rebalancing and backtests | 2.5 weeks |
| Hardening | 16–19 | fast, observable, trustworthy | 2 weeks |
| Differentiate + ship | 20–21 | API, alerts, launch | 1.5 weeks |

**The critical path is Prompts 3–6.** If the data is wrong or the screener semantics are subtly
off, everything built on top of it is confidently wrong — which is worse than broken. Do not let
those four modules through review without their golden tests passing.
