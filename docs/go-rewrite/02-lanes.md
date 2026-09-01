# 02 — Lanes

Nine terminals. **L0 is Maulik's** (foundation, then integrator). L1–L8 are developers. Each
lane's Python scope is listed as the files it must read *in full* before writing Go; its Go
scope is the directories it owns (01 §Layout). "Must" is what G1/G2 grade; "should" is wave 2;
"could" is wave 3 or the next evening. Dependencies say who a lane waits on — and the rule is
**never wait idle**: if the dependency is not merged, code against the interface in
`REQUESTS.md`, stub it, keep going.

## L0 — Foundation, then integrator (Maulik + Cowork)

**Wave 0 (T+0:00–0:45), solo, must finish first:**

1. `go/go.mod` (`github.com/maulikam/baskfy`, go 1.23), `Makefile` (`build test lint sqlc oapi golden`),
   `.golangci.yml` with the `depguard` rules from 01 §Laws, `cmd/{api,worker,desk,baskfy}/main.go`
   that start, log, and exit 0.
2. `internal/domain`: `Symbol`, `ISIN`, `Date` (civil date, no clock), `Clock`, `IST`, `Money`
   (= `decimal.Decimal` newtype with `Round2`), `Bar`, `Series`, `Panel`, `UserID`,
   `BrokerAccountID`, `PlanID`, `Exchange`, `Product` (CNC/MIS), `Segment` (NSE/NFO/BFO),
   `DecileBucket` (`decile_1…decile_6`, the D1–D6 public contract), `Regime`, `Window`. Read
   `packages/core/src/baskfy_core/models/*.py`, `precision.py`, `windows.py`, `universes.py`,
   the desk `config.py` first — those are the vocabulary. **Frozen at G0**; add-only after.
3. `internal/config`: one struct, every env name from `.env.example` and desk `config.py`
   (`DRY_RUN` defaults **true**; `RISK_*` ceilings carried verbatim).
4. `internal/db`: `pgx` pool from `BASKFY_DATABASE_URL`; `schema.sql` = `pg_dump --schema-only
   --no-owner` of the staging/dev DB (both the app schema and the desk schema — see
   `services/api/desk_schema.py`); `sqlc.yaml` pointing at `queries/*.sql` → `gen/`; one
   example query per lane file so the layout is proven; `Tx` helper.
5. `internal/obs`: slog JSON, otel init from `BASKFY_OTEL_*`, prometheus registry, sentry.
6. `internal/testkit`: `LoadGolden[T](t, "L1/score/case_001.json")`, `TestDB(t)` (truncates
   between tests, uses `BASKFY_TEST_DATABASE_URL`), `FixtureCSV`, `ApproxEqual(a, b, tol)`.
7. `internal/core/laws_test.go` (import-graph test) and `internal/execution/laws_test.go`
   (no live broker compiles).
8. `.github/workflows/go.yml`: build, lint, test with the Postgres/Redis service containers CI
   already has. Extend `tools/ci-local.sh` with a `[go]` job.
9. Tag `go/main` `G0`, write `STATUS.md` row, ping all lanes to rebase.

**Waves 1–3, integrator:** answer `REQUESTS.md` within 15 min (domain additions are yours);
run `tools/rewrite/merge-lane.sh L<n>` at every gate for every lane that reports green;
keep `go/main` always building; write the gate line in `STATUS.md`. Never write lane code —
if a lane is stuck, re-scope it (fold table below), don't take over.

## L1 — core/signals (the answer key)

**Python, read in full:** `packages/core/src/baskfy_core/{factors,score,momentum,momentum_scan,
scan_projection,basket,basket_sizing,screener,screen_definition,screen_definition_corpus,
screen_definition_schema,screen_diff,rank_buffer,windows,universes,breadth,precision,
trading_calendar,circuits,costs,blends,factor_registry,instrument_regime,risk_free}.py`;
`decile-blueprint/docs/05-factor-formulas.md`; `kite-momentum-rebalancer/app/scoring.py`
(the 29-column seam) and `scan_source.py`; `decile-blueprint/reconciliation/`; tests under
`packages/core/tests/test_{factors,score,basket,screener,momentum*,rank_buffer,precision}*.py`.

**Go:** `internal/core/signals`.

- **Must:** `factors.go`, `score.go`, `momentum.go`, `momentum_scan.go`, `basket.go`,
  `basket_sizing.go`, `precision.go`, `windows.go`, `rank_buffer.go`, `trading_calendar.go`
  (calendar *data* passed in, never fetched), `factor_registry.go`. Goldens: the momentum scan
  over `kite-momentum-rebalancer/data/uploads/sample_scan.csv` and `scan_1787143663_*.csv`
  (read-only corpus) — **all 29 desk columns and all 93 screener columns byte-equal after
  rounding**; `reconciliation/` cases; every fixture in `packages/core/tests/fixtures`.
- **Should:** `screener.go`, `screen_definition*.go` (JSON schema validation via
  `screen-definition.schema.json`), `screen_diff.go`, `universes.go`, `breadth.go`,
  `instrument_regime.go`, `circuits.go`, `costs.go`, `blends.go`, `risk_free.go`, `scan_projection.go`.
- **Could:** property tests (rapid) for monotonicity of rank/buffer and the hold band.
- **Deps:** L0 domain only. L6 and L8 consume you — export early, even if internals change.

## L2 — core/backtest + regime + actions + ledgers

**Python:** `backtest.py` (1,825), `backtest_metrics.py`, `exposure/regime.py` (1,184),
`exposure/allocation.py`, `adjustments.py`, `action_recovery.py`, `reconcile.py`,
`reconciliation.py`, `sleeves.py`, `allocation_ledger.py`, `cash_ledger.py`; desk
`app/analytics/{regime_run,regime_store,protection,tax_lots}.py` for how the regime is *used*;
`benchmarks/results/`; `decile-blueprint/docs/DECISIONS.md` §21.9–21.10 (the two known-bugs).

**Go:** `internal/core/{backtest,regime,actions,ledger}`.

- **Must:** `regime/regime.go` + `allocation.go` (the desk's live overlay — goldens from the
  desk's regime tests, which are the 95 %-covered ones); `actions/adjustments.go` reproducing
  §21.9 *as-is* with a `known_bug` golden marker; `ledger/cash_ledger.go`, `allocation_ledger.go`.
- **Should:** `backtest/backtest.go` + `metrics.go` with goldens from `benchmarks/results` and
  the API's backtest tests; `ledger/reconcile.go`, `reconciliation.go`, `sleeves.go`.
- **Could:** `actions/action_recovery.go`.
- **Deps:** L1's `signals` for the scoring step inside the backtest (stub with a golden-fed
  fake until L1 merges at G1).

## L3 — core/product (curated, portfolio, billing, accounts)

**Python:** `curated_*.py` (10), `entitlements.py`, `manager_onboarding.py`, `sebi_registration.py`,
`portfolio_{csv,graph,nav,units}.py`, `cas_import.py`, `grouping_suggestions.py`, `pros_cons.py`,
`gst.py`, `invoice.py`, `pdf.py`, `api_keys.py`, `broker_connections.py`, `tenancy.py`,
`public_api.py`, `seed_data.py`; `docs/smallcase/04-business-rules.md`.

**Go:** `internal/core/{curated,portfolio,billing,accounts}`.

- **Must:** `portfolio/nav.go`, `units.go`, `csv.go` (the web app's most-used route group is
  `portfolios`); `billing/gst.go`, `invoice.go` (fees `min(₹100, 1.5 %) + GST`, computed not
  collected); `accounts/{api_keys,tenancy}.go`.
- **Should:** `curated/{accounting,drift,metrics,plans,sip,versions,dividends}.go` (Track B is
  dark — flags false — so parity matters more than speed here), `portfolio/graph.go`,
  `cas_import.go`.
- **Could:** `curated/trending.go`, `grouping_suggestions.go`, `pros_cons.go`, `pdf.go`,
  `seed_data.go`, `public_api.go`.
- **Deps:** L0 only. **First lane to fold** if terminals are short.

## L4 — providers + execution (wave 1), desk console (waves 2–3)

**Python:** `packages/providers/src/baskfy_providers/*.py` (17), `packages/execution/src/
baskfy_execution/*.py` (9), desk `app/kite_client.py`, `token_store.py`, `core/{gateway,net,
websec,ticker}.py`, `scripts/token_sync.py`; then for waves 2–3: `app/main.py` (1,266, the 33
routes), `app/analytics/*.py`, `app/config.py`, `rebalance.py`, `breadth_source.py`,
`templates/*.html`, `scripts/{daily,autorun,friday_drill,shadow_mode,backup}.py`.

**Go:** `internal/providers`, `internal/execution`; then `internal/desk`, `web/desk`, `cmd/desk`.

- **Must (G1):** `providers/{ratelimit,circuit,retry,tokens,records,ports}.go`; `providers/kite`
  (instruments, historical with `BASKFY_KITE_MAX_DAYS_PER_REQUEST` chunking, holdings with
  `quantity + t1_quantity + collateral_quantity`, margins) behind the token bucket in Redis
  **with Python's exact key names**; `providers/nse` (bhavcopy, indices, corporate actions,
  cookie discipline); `execution/{gateway,guards,risk,ratelimit,journal,tenancy,broker_ports}.go`
  + `adapters/dryrun.go`; **the seven non-negotiables as seven named tests** in
  `execution/nonnegotiables_test.go`; `StopFromVol` 8–12 %; GTT stop through the gateway.
- **Should (G2):** `providers/{archive,composite,fixtures,fixture_builder}.go`;
  `desk/` `/analyze` → plan (30-min expiry, `plan_id`) and `/execute` (`confirm=true`, DRY_RUN
  only) as JSON; `/status`, `/settings`, `/reconcile/data`, `/stops`, `/indices/data`,
  `/regime/data`, `/performance/data`, `/tradebook/data` reading the **desk Postgres schema**.
- **Could (G3):** the HTML pages (`html/template` from the 12 Jinja templates, static copied),
  `/ops`, `/options/*` (reads only; strangle stays frozen), `daily`/`autorun`/`friday_drill`
  equivalents as `cmd/baskfy desk …` subcommands.
- **Deps:** L0. L2's `regime` for the exposure overlay in `/analyze` (interface + fake until G1).

## L5 — API platform (skeleton, auth, billing, admin, alerts, webhooks, email)

**Python:** `services/api/src/baskfy_api/{app,settings,logging,problems,csrf,security,ratelimit,
idempotency,http_cache,openapi,auth,auth_google,auth_service,admin,api_keys,billing,razorpay,
invoices,webhooks,entitlements,alerts,email/*,telemetry,sentry,track_b,queue,db}.py`;
`routers/{auth,admin,api_keys,billing,alerts,meta,public,support,track_b,webhook_endpoints,
managers}.py`; `schemas.py`; tests under `services/api/tests/test_{auth,admin,billing,alerts,
webhooks,api_keys,ratelimit,csrf,idempotency}*.py`.

**Go:** `internal/api/{server,app,auth,platform}`, `cmd/api`.

- **Must (G1):** `server/` generated from `openapi.json` (`oapi-codegen` strict server, chi);
  `app/` mounts **every** operation with a `501 problem+json` default so L6/L7 fill in
  handler-by-handler; middleware chain identical in order to `app.py` (request id → logging →
  otel → CORS from `BASKFY_CORS_ORIGINS` → rate limit (Redis, same keys) → auth → csrf →
  idempotency → http cache); `auth/` (HS256 verify with the Next.js-issued tokens; argon2id;
  refresh tokens; lockout); `routers/meta` (the web app calls `meta` 12×); `problems.go`.
  Contract test: every operation in `openapi.json` is routed (a table test over the spec).
- **Should (G2):** `admin` (11 web refs), `alerts` (6), `api_keys` (`keys` 4), `billing` +
  `razorpay` webhooks + `invoices` (3), `support`, `public`, `webhook_endpoints`, `managers`,
  `email` (Resend + the 568 lines of templates as `text/template`).
- **Could:** `track_b` (flags false), `auth_google`, `public_docs`.
- **Deps:** L0. **Owns the `server/` package** — L6/L7 implement `StrictServerInterface`
  methods in their own packages and register through `app/registry.go` (one line per handler
  group) to avoid merge conflicts.

## L6 — API market + backtests + portfolios (what the web app hits most)

**Python:** `services/api/src/baskfy_api/{screener,search,instruments,market_data,query_plans,
csv_export,integrity,metrics,backtests,backtest_runner,baskets,portfolios,curated_tenant}.py`;
`routers/{screens,explore,instruments,market_data,search,backtests,baskets,portfolios,sleeves,
portfolio_overview}.py` (`portfolio_overview.py` alone is 3,860 lines — read it twice);
tests `test_{screens,explore,instruments,market,backtests,baskets,portfolios,sleeves,overview}*.py`.

**Go:** `internal/api/{market,backtests,portfolios}`.

- **Must (G1):** `portfolios` (21 web refs, 15 spec ops), `screens` (20 / 10), `explore` (4 / 9),
  `instruments`, `search` — handlers + `queries/L6_*.sql`; contract tests ported from the
  httpx tests (same request → same status + same JSON shape).
- **Should (G2):** `backtests` (10 / 9) enqueueing River jobs (L8 consumes), `market_data`,
  `sleeves`, `portfolio_overview` (the single biggest file in the API — split by section:
  `overview_{nav,holdings,sleeves,history}.go`), `csv_export`, `http_cache` interplay.
- **Could:** `integrity`, `query_plans`, `metrics` endpoint parity.
- **Deps:** L5's `server/` + `app/registry.go` (G0+30 min — L5 generates the server first
  thing); L1's `signals` (screens); L3's `portfolio` (nav). Stub via goldens until merged.

## L7 — API curated + brokers + desk routes

**Python:** `services/api/src/baskfy_api/{curated_catalogue,curated_investments,
curated_metrics_service,curated_seed,curated_versions,broker_accounts,broker_holdings,
broker_oauth,kite_basket,desk_schema}.py`; `routers/{curated_*}.py` (10), `routers/{brokers,kite,
desk}.py`; `docs/smallcase/02-scope-and-gating.md` (**Track A/B/C** — the web app never gains
an execute route).

**Go:** `internal/api/{curated,brokers,desk}`.

- **Must (G1):** `brokers` (5 web refs) incl. OAuth callback + holdings sync read path;
  `kite_basket` (the Kite Publisher hand-off, ≤10 instruments — M52); `routers/desk` read-only
  endpoints (`/baskets`, `/baskets/plan` pages depend on them).
- **Should (G2):** `cb` catalogue + investments + versions + metrics service (9 web refs).
- **Could:** `curated_{engage,sip,drift,trending,costs,customize,create,from_screen}` routers,
  `curated_seed`.
- **Deps:** L5 `server/`, L3 `curated`, L4 `providers/kite`. **Second lane to fold.**

## L8 — worker: scheduler, jobs, pipeline, backfills, tasks

**Python:** `services/worker/src/baskfy_worker/**` (54 files; `celery_app.py` for the 18
schedules, `orchestrator.py` + `steps.py` + `engine.py` for the nightly pipeline, `tasks/*`);
desk `scripts/{daily,autorun,backup}.py` and `app/analytics/{daily_runs,ops,snapshot}.py`
(the desk's own 18:30/18:50 jobs); `docs/09` schedule notes referenced in `celery_app.py`.

**Go:** `internal/worker/**`, `cmd/worker`, `cmd/baskfy` subcommands.

- **Must (G1):** `scheduler/` with all 18 entries in IST (a test asserts the cron table
  equals `BEAT_SCHEDULE` — dump it as a golden); `jobs/` (River client, `river` schema,
  job names = Celery task names `baskfy.pipeline.nightly` …); `pipeline/` orchestrator + steps
  + engine + `pipeline_run`/`pipeline_run_step` rows identical to Python's (a golden of a
  completed run's rows); `tasks/{instruments,bars,factors,membership,snapshots,publish}.go`.
- **Should (G2):** `backfill/*` (resumable from `ingest_cursor`, 2011-01-01, Kite 3 req/s via
  L4's bucket), `tasks/{corporate_actions,adjustments,quality,market_health,holdings_sync,
  portfolio_nav_job,alerts,backtests}.go`, `cli/` subcommands, the desk `daily`/`autorun` jobs
  **in DRY_RUN with a hard stop before any order path**.
- **Could:** `tasks/{curated_*,purge_accounts,fundamentals,listings}.go`, `ops.go` (reap,
  deadline, token, backlog checks).
- **Deps:** L0; L4 providers (G1); L1 signals (G1). Until then run the pipeline with the
  `providers/fixtures` fake against goldens.

## Folding (fewer than nine terminals)

| Terminals | Run tonight | Folded, next evening |
|---|---|---|
| 9 | L0–L8 | — |
| 8 | L0–L2, L4–L8 | L3 → L2's terminal |
| 7 | L0, L1, L2, L4, L5, L6, L8 | L3 → L2; L7 → L6 |
| 6 | L0, L1, L4, L5, L6, L8 | L2 → L1 after G1; L3, L7 as above |

A folded lane's Python stays live in the strangler; nothing breaks, it just moves later.
