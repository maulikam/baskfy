# 06 — Module plan: SC0–SC12

One commit per module, `SC<N>: green — <one line>`. A module is green when its acceptance
criteria pass as tests (or, where a criterion is a human-visible surface, when the page
renders in the dev stack and a browser/E2E check covers it), `make lint` is clean in the
touched trees, both existing suites still pass, and `STATUS.md` + (if judgement was
exercised) `DECISIONS-SC.md` are updated. Criteria are proxies for Goals — the charter's
precedence order applies.

Dependencies are mostly linear; SC7/SC8/SC9 are independent of each other and may be
reordered if one blocks.

---

### SC0 — Baseline and read-in

**Goal:** a fresh session knows exactly where it stands and cannot damage what exists.

- Read the read-order docs; read `docs/smallcase/STATUS.md` and resume from the first
  non-green module if this is a resumed run.
- Record in STATUS: repo/branch state, both suites' pass counts, the live schema's
  relevant tables (instruments/prices/journal/holdings names actually found), where the
  M22+ `/baskets` pages live, and the Celery Beat schedule inventory.
- Verify `DRY_RUN=true` in every env file an agent touches; verify the desk suite is green
  before writing anything.
- **AC:** STATUS.md's SC0 section lets a reader with no other context name the tables and
  suites this run builds on; both suites green at baseline.

### SC1 — Schema and domain objects

**Goal:** the `cb_` schema of `03-data-model.md`, migrated, seeded, idempotent.

- Migrations for all `cb_` tables (Track-B tables included, dormant); seed managers
  (`baskfy-engine`, `maulik`) and `BASKFY_SOLE_USER_ID`.
- Domain objects/repositories in the service layer; pure validation (weights sum to 1,
  version immutability) in `packages/core`-style code with no I/O.
- Reconcile with what M22 already built for `/baskets` — extend, don't duplicate; record
  the mapping in DECISIONS-SC.
- **AC:** migrate → seed → migrate again is a no-op (idempotent); inserting a version with
  weights ≠ 1.0 or mutating a written version fails a test; re-running seeds changes no
  rows.

### SC2 — Catalog computation and API

**Goal:** every number on a card is computed, stored, and served.

- `packages/core` pure functions: min-amount, share-allocation, chain-linked version-aware
  return series, CAGR/window metrics, volatility (value + bucket per `04` §3).
- EOD metrics job on Celery Beat (idempotent per date) filling `cb_metrics`; on-demand
  recompute path for plan previews.
- Catalog API: list with query-param filters/sorts exactly as `05-ui-spec.md` defines,
  detail, manager, collections, watchlist CRUD.
- First SCAN basket seeded: project the current MomentumScan strategy output into
  `cb_basket(source=SCAN)` + genesis version (deterministic projection, tested against a
  fixture scan).
- **AC:** property test from `04` §2 (min-amount buys ≥1 share of everything); metrics job
  re-run for a date is a no-op; a golden-file test pins the return series for a fixture
  basket over fixture prices; filter/sort API round-trips every documented query param.

### SC3 — Versions, rebalance engine, plans

**Goal:** publishing and applying versions works end-to-end against the desk.

- Version publishing (manual + from-scan), diff computation (holdings vs new version per
  `04` §5), timeline API.
- Plan generation: diff → desk `/analyze`-shaped plan for invest / invest-more / apply /
  exit / partial-exit, with min-amount/top-up handling; batch lifecycle
  DRAFT→PLANNED→EXECUTED/EXPIRED synced from the desk journal by `desk_plan_id`.
- Market-hours guard on every plan-generating endpoint (calendar-driven, IST).
- Publishing side-effects: update post, pending actions, per-user rebalance state.
- **AC:** in DRY_RUN, the full loop runs in a test: seed basket → invest plan → simulated
  journal fill → holdings ledger correct → publish v2 → diff preview matches hand-computed
  fixture → apply plan → state APPLIED; plan expiry test (31 minutes → EXPIRED, state back
  to PENDING); out-of-hours request returns the closed-market payload, never a plan.

### SC4 — Investment accounting

**Goal:** the investor math of `04` §4 and §7, correct to the rupee.

- Ledgers: money put in, current investment, realized PnL on sells, XIRR (with the >365d
  display rule), per-constituent returns.
- Dividends: derive `cb_dividend` from the corporate-actions table × holdings history;
  totals surface per investment.
- Fee ledger: entries per `04` §1 written on batch execution, `collected=false`.
- Drift job + fix-flow per `04` §7 against the desk holdings snapshot.
- **AC:** XIRR matches a hand-computed fixture to 4 decimals; realized-PnL fixture with a
  partial exit and a loss; dividend derivation matches RECOVERED-ACTIONS fixtures; drift
  test (simulated direct sale) raises the action and fix re-bases with a synthetic exit;
  fee rows match `04` §1 for boundary amounts (₹6,666 and ₹7,000 straddle the 1.5% cap).

### SC5 — Web UI: discovery and detail

**Goal:** `/explore`, `/basket/[slug]`(+constituents), `/manager`, `/collections`, and the
shared components of `05-ui-spec.md`.

- Includes the performance chart with SIP mode and benchmark compare; disclosure
  components on every performance surface; the merge-or-redirect decision for the legacy
  `/baskets` pages.
- **AC:** E2E: filter via URL params → card → detail → timeline renders the fixture
  basket's versions; SIP toggle and compare change the series; disclosure block present on
  every route that shows a return; legacy catalog resolved (no two competing catalogs).

### SC6 — Web UI: investor surfaces

**Goal:** `/investments`, `/investments/[id]`, orders read-only, `/watchlist`, `/fees`,
pending actions, and the home-page modules.

- **AC:** E2E against DRY_RUN fixtures: dashboard totals equal SC4's ledgers; Show-Details
  modal matches the accounting test fixtures; every order-shaped CTA ends in
  PlanHandoffPanel or MarketClosedModal — **and the no-order-route test is extended to
  every new route** (grep-level and route-table-level, per M23's precedent).

### SC7 — SIP reminders

**Goal:** `cb_sip_plan` with REMINDER mode: schedule, next-fire computation
(holiday-aware), Beat job that raises SIP_DUE pending actions + bell notification, pause /
resume / delete, and the invest-more plan prefilled from the reminder.

- **AC:** calendar test (fire date rolls forward over a holiday weekend); firing is
  idempotent per (plan, month); no code path places an order (AUTO mode absent from the
  enum's write path, asserted).

### SC8 — Create and customize

**Goal:** `/create` per `05-ui-spec.md`: instrument search, ≥2 constituents, equal/custom
weights with normalization, backtest preview via existing machinery, save as PRIVATE
basket, investable via the SC3 plan path; "manage constituents" customize-flow on existing
investments (CUSTOMIZE batches).

- **AC:** E2E: build → preview → save → private basket appears in catalog (visibility
  respected: PRIVATE never in public lists) → invest plan generated; weights normalize to
  1.0; preview uses point-in-time data (no look-ahead — reuse the screener's assertion
  pattern).

### SC9 — Engagement: updates, trending, collections, notifications

**Goal:** the feed and ranking layer, honest about single-tenant reality.

- Engine auto-posts per version (template: what changed + regime context); manual post
  path for the human manager; unread-dot state.
- Trending jobs: computable rankings only (top by 1M/1Y return, recently rebalanced,
  budget-friendly by min-amount, most-watched degenerates to watchlist recency) — each
  list labeled with what it actually ranks; no fake "most invested".
- Seed 4–6 collections from existing strategy families; in-app bell with badge count
  backed by pending actions + posts.
- **AC:** publishing a version creates exactly one post and one bell item per affected
  user; ranking jobs idempotent; collections render from data with zero hardcoded slugs
  in components.

### SC10 — Gating middleware and Track-B dark machinery

**Goal:** the access matrix of `02` implemented once; subscriptions/entitlements built
dark.

- Middleware: web-login, broker-session, subscription-entitlement, market-hours checks as
  composable guards; applied per the matrix.
- Track B: `cb_plan`/`cb_subscription` service layer, lock states in UI, paywall routes —
  all behind `BASKFY_SUBSCRIPTIONS_ENABLED=false`; sign-up routes behind
  `BASKFY_PUBLIC_SIGNUP_ENABLED=false`.
- **AC:** flag-off tests: every fee-based lock renders as Free Access, paywall and sign-up
  routes 404, no navigation path reaches them; flag-on (test env only) shows the lock on a
  FEE fixture basket; broker-session guard returns the stale-session state when the token
  bridge reports expiry.

### SC11 — Hardening and the safety proof

**Goal:** the run's safety claims become tests and documentation.

- The no-order-route test suite finalized (web app cannot reach `packages/execution`'s
  order path: import-graph assertion + route assertion + HTTP-level probe in CI).
- Load sanity on `/explore` and metrics job over the full 2011→now history (the /baskets
  67-second lesson — budget: catalog p95 < 1s on dev hardware, jobs bounded).
- Runbook entry: how to publish a version, how to apply on Friday, how to fix drift, how
  to flip Track-B flags (deliberately manual).
- `RUN-AND-TEST.md` extended with the SC stack's bring-up and test commands.
- **AC:** CI green including the new suites; runbook section exists; a cold `make`-level
  bring-up of the dev stack renders `/explore` with seeded data.

### SC12 — Verification pass and final report

**Goal:** prove the seven flows of `01-requirements.md` §D, then write the report.

- Walk all seven flows in the dev stack (DRY_RUN), screenshotting or logging each;
  discrepancies against `04` become fixes or honest open items.
- Sweep: every ⚠ UNREVIEWED decision listed; NEEDS-MAULIK updated; STATUS closed out.
- Write `SC-FINAL-REPORT.md` at the repo root in FINAL-REPORT.md's register: what was
  built, what was decided, what is NOT done, what needs Maulik (expected entries: D3/D7
  before any flag flips; review of volatility thresholds and fee display copy; the
  decision on charging real fees to himself is a joke — but the ledger's accuracy isn't).
- **AC:** the report exists and is honest; the repo is one flag-flip + D3 away from
  Phase 4, and says so precisely.
