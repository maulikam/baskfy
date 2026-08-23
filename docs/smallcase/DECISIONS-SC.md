# DECISIONS-SC — judgement calls of the smallcase run

Same convention as `docs/DECISIONS-MERGE.md`: numbered by module, each entry records the
context, the choice taken, the rejected alternatives and why, and how to reverse it.
Decisions made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Two decisions were pre-taken in the pack itself, so the run doesn't stall on them — they
are recorded here for review like any other:

## PACK.1 — Volatility thresholds under single-tenant reality · ⚠ UNREVIEWED

`04-business-rules.md` §3: smallcase buckets by comparing across its whole catalog; with a
handful of published baskets terciles are meaningless. Fixed annualized thresholds
(LOW < 15% ≤ MED < 25% ≤ HIGH) apply until 12 baskets are PUBLISHED, then terciles take
over. Rejected: terciles from day one (degenerate), copying smallcase's unknown internal
cutoffs (unknowable). Reversal: constants in one config location; recompute job re-buckets
everything on change.

## PACK.2 — Web app generates plans but never executes · (not reversible this run)

`02-scope-and-gating.md` Track C: this is the charter's D3 gate plus desk non-negotiable
#1 restated for the new surfaces, not a fresh judgement — recorded here so no later module
"discovers" flexibility in it. Reversal path exists only through a written D3 answer in
`docs/DECISIONS-MERGE.md`, after which the execution-in-web design becomes its own planned
phase (it is NOT part of SC0–SC12).

---

(Module entries follow, newest at the bottom.)

## SC0 — baseline: M41 sits beside the SC run, not inside it ⚠ UNREVIEWED

**Context.** SC0 found uncommitted M41 broker-catalog work and the `docs/smallcase/` pack on
the same dirty tree. The broker grid is product furniture the SC UI will eventually link;
live OAuth is already D3-gated.

**Taken.** Treat M41 as a **merge-track** deliverable (commit `M41: green — …`) and SC0 as
**docs-only baseline** (commit `SC0: green — …`). SC modules do not absorb M41's gate or
`/brokers` page into `cb_*` schema.

**Rejected.** Squashing M41 into SC0 (muddies the two ledgers). Deleting M41 to get a clean
tree (throws away the connect UI the product ask wanted).

**Reversal.** Revert the M41 commit; SC0 STATUS baselines still hold.

## SC1 — extend M22 `/baskets`, do not duplicate · ⚠ UNREVIEWED

**Context.** M22/M30 already serve `/baskets` from `basket_snapshot` — a nightly JSON cache of the
live MomentumScan build. SC1 introduces `cb_basket` as the curated **product** layer (managers,
versions, constituents, collections, investments). The two must coexist until SC5 merges the read
surfaces.

**Taken.** **Extend, don't duplicate.** `basket_snapshot` and `GET /api/v1/baskets` stay as the desk
operator's live scan basket. `cb_basket` with `source=SCAN` is the catalog projection cut from those
runs (deterministic: same scan in → same version out). `source=MANUAL` covers operator- and
user-created private baskets. No second MomentumScan page or duplicate instrument table in SC1–SC2.

**Rejected.** Renaming or dropping `basket_snapshot` in SC1 (breaks the desk before SC5 is ready).
Mirroring every nightly snapshot into `cb_basket_version` immediately (SC3's rebalance engine owns
the cut). A separate `cb_instrument` table (house rule: join the screener's `instrument` master).

**Reversal.** Drop `cb_*` migration 0014 and models; `/baskets` continues unchanged.

## SC1 — weight sum enforced in domain, not Postgres · ⚠ UNREVIEWED

**Context.** docs/smallcase/03 asks for `CHECK sum(weight) per version = 1.0000` but Postgres cannot
express a per-parent aggregate check without triggers.

**Taken.** Service-layer assertion via `baskfy_core.curated_baskets.assert_weights_sum_to_one`,
tested in `test_curated_baskets.py` and `test_curated_schema.py`. Constituent weight storage is
`numeric(7,4)`; tolerance `0.00005` after quantize-to-4dp.

**Rejected.** A deferrable trigger in SC1 (more moving parts before the insert path exists in SC3).
Storing unnormalized weights and fixing at read time (violates house rule 8).

**Reversal.** Add a trigger migration; keep the pure assert as a fast-fail before the DB round-trip.

## SC1 — enum casing matches the smallcase spec · ⚠ UNREVIEWED

**Context.** Billing `plan`/`subscription` enums in docs/04 use lowercase (`active`, `month`). The
smallcase pack uses uppercase (`ENGINE`, `FREE`, `PUBLISHED`).

**Taken.** All `cb_*` check constraints use the uppercase vocabulary from docs/smallcase/03 verbatim.
The dormant billing tables are untouched.

**Rejected.** Lowercasing cb enums to match docs/04 (would diverge from the product spec and every
mock in the smallcase pack).

**Reversal.** One migration to rewrite constraints and seed rows; no production data yet.

## SC1 — sole user resolved from env or e2e account · ⚠ UNREVIEWED

**Context.** Track A is single-tenant; every user-scoped row carries `user_id` equal to
`BASKFY_SOLE_USER_ID` (docs/smallcase/02).

**Taken.** `SOLE_USER_ENV = "BASKFY_SOLE_USER_ID"` in core; `baskfy_api.curated_seed.resolve_sole_user_id`
reads the env var when set, otherwise upserts the e2e account via `seed_e2e_account` and returns its id.
SC1 seeds managers only — no `cb_investment` rows until SC4.

**Rejected.** Hard-coding user id `1` (breaks fresh databases where the first user is not id 1).
Inferring "the only user" implicitly in queries (forbidden by docs/smallcase/03 rule 1).

**Reversal.** Delete `curated_seed.resolve_sole_user_id`; callers pass explicit ids in tests only.

## SC2 — catalog at `/api/v1/explore`, not `/cb/baskets` · ⚠ UNREVIEWED

**Context.** Module plan allowed `/api/v1/explore` or `/api/v1/cb/baskets`. UI spec routes are
`/explore`, `/manager`, `/collections`, `/watchlist`. M22 already owns `/api/v1/baskets` (live
scan). Paths containing the substring `basket` are asserted GET-only by
`test_baskets_readonly.py`.

**Taken.** Prefix **`/api/v1/explore`** for list/detail/managers/collections; watchlist at
**`/api/v1/watchlist`** (mutating verbs allowed; not under M22's `basket` substring gate).
Documented list query params (05 chips/dialog mapped): `max_min_amount`, `access`, `volatility`,
`category`, `rebalance_frequency`, `basket_type`, `include_new`, `sort`, `order`, `q`.
`sort` ∈ {min_amount, ret_1y, cagr_3y, cagr_5y, name, launched_at, volatility}.

**Rejected.** `/api/v1/cb/baskets` (diverges from UI paths). Putting watchlist under
`/explore/baskets/...` (would break M22's GET-only assertion without narrowing that test).

**Reversal.** Rename routes in `routers/explore.py` and regenerate the client; keep M22 paths.

## SC2 — float only at the annualisation / CAGR boundary · ⚠ UNREVIEWED

**Context.** House rule 9: money is Decimal. Sample std-dev and fractional powers are not money.

**Taken.** Prices, weights, min-amount, NAV steps, and stored return percents stay `Decimal`
(half-up at write). `annualized_volatility` and `cagr` convert to float only for `math.sqrt` /
`** (1/years)`, then immediately re-enter `Decimal` and quantize (10 dp for vol fraction, 2 dp
for percent returns).

**Rejected.** Pure-Decimal series expansion for CAGR (complexity, no gain at 2 dp). Storing
float in `cb_metrics` (violates schema Numeric columns).

**Reversal.** Swap the two helpers behind the same signatures; re-run the metrics job.

## SC2 — EOD metrics Beat at 20:20 IST · ⚠ UNREVIEWED

**Context.** Metrics need published closes; publish SLO is 20:15; alerts fire at 20:30.

**Taken.** `cb-eod-metrics` → `baskfy.cb.compute_metrics` Mon–Fri **20:20** IST on the compute
queue. Idempotent upsert on `(basket_id, as_of_date)`.

**Rejected.** Bundling into `pipeline.nightly` (alerts/publish failure modes differ). Running
before 20:15 (risk of incomplete bars).

**Reversal.** Change one Beat entry; task name stays.

## SC5 — Explore is public catalog; `/baskets` stays desk MomentumScan · ⚠ UNREVIEWED

**Context.** SC1 deferred the M22 `/baskets` vs curated-catalog collision to SC5. UI spec
(`05`) said merge-or-redirect so two competing catalogs do not remain. Soft redirect was
preferred over a hard HTTP redirect so desk operators keep a Friday MomentumScan surface.

**Taken.** **Explore is the public catalog; `/baskets` remains the desk MomentumScan operator
view.** `/explore` + `/basket/[slug]` (+ constituents stub) read `GET /api/v1/explore`. Nav gains
Explore. `/baskets` is **not** hard-redirected: a notice + primary CTA to `/explore` soft-steers
product discovery while the live scan table and `/baskets/plan` stay for operators. Aligns with
SC1 "extend, don't duplicate" — `basket_snapshot` / `GET /api/v1/baskets` unchanged. Web still has
**no order route**; Invest CTAs open `PlanHandoffPanel` (or `MarketClosedModal`).

**Rejected.** Hard redirect `/baskets` → `/explore` (breaks desk Friday workflow and M22 plan
page adjacency). Deleting `/baskets` (throws away the live scan UI before SC3 versions fully
replace the operator need). Merging both into one page (confuses catalog cards with today's
ranked scan).

**Reversal.** Remove the `/baskets` banner and Explore nav entry; leave `/explore` routes as
orphans or delete them in a follow-up. Hard redirect can replace the soft notice later if
operators no longer need the scan table.

## SC4 — Fee rounding order and XIRR day-count · ⚠ UNREVIEWED

**Context.** docs/smallcase/04 §1 says `base = min(₹100, 1.5% × amount)` then 18% GST, round
half-up to 2 dp at write. Boundary fixtures ₹6,666 / ₹7,000 straddle the cap
(`6666 × 0.015 = 99.99`, `7000 × 0.015 = 105 → 100`). XIRR must match a hand fixture to 4 dp;
display only when first investment is **>365** calendar days old.

**Taken.** Quantize the uncapped `amount × 1.5%` with `money()` *before* applying the cap, then
GST on the capped base, then `money(base + gst)`. XIRR is ACT/365 Newton + bisection,
quantized to 4 dp; sign convention invest-negative / redeem-positive (Excel XIRR). Pure module
`baskfy_core.curated_accounting` — services journal `collected=false`; no collection this run.
Drift shortfall → `DRIFT` pending-action shape in `baskfy_core.curated_drift`; fix archives a
synthetic EXIT at ledger avg cost then re-bases qty to broker (04 §7). Excess → CUSTOMIZE ids
only (not a DRIFT action). Dividend row derivation from corporate actions stays in the API/worker
service (needs holdings history + CA table) — not invented in core.

**Rejected.** Cap-then-round without quantizing the rate product first (paisa drift on odd
amounts). Float XIRR (house rule 9). Showing XIRR at exactly 365 days (spec is strictly greater).

**Reversal.** Change the two fee lines and the day-count constant; re-run
`test_curated_accounting.py` / `test_curated_drift.py`. Fee ledger rows already stored keep their
written numbers (no rewrite).

## SC3 — apply-preview diffs holdings (not prior version); value-delta floor · ⚠ UNREVIEWED

**Context.** docs/smallcase/04 §5: apply preview diffs *intended holdings* against the new
version's target weights at current prices → buy/sell child list; sells fund buys; residual
cash line; top-up may be required for new min-amount. Two whole-share algorithms are plausible:
(a) qty-first `floor(V × w / p)` then delta vs held qty; (b) value-delta
`floor(|target_value − current_value| / p)` capped by held qty.

**Taken.** **(b) value-delta floor** in `baskfy_core.curated_versions.diff_holdings_vs_weights`.
`top_up = max(0, −residual, min_amount − portfolio_value)` so a negative residual (sells under-fund
buys after flooring) and a min-amount shortfall are both covered. Version numbers are strictly
sequential (`assert_next_version_no`); publish side-effects are a pure `PublishSideEffects`
description (ENGINE post + `REBALANCE_AVAILABLE` + `PENDING`) persisted by
`baskfy_api.curated_versions` — no desk plan / no gateway. API unit tests live in
`test_curated_versions_service.py` (basename collision with core under pytest import mode).

**Rejected.** Qty-first target allocation (changes sell qty on the hand fixture B leg 2→3 and
hides residual under-funding). Top-up = min-amount gap only (leaves negative residual unfunded
in the preview). Plan generation in this leaf (owned by 1.2.2).

**Reversal.** Swap the floor loop in `diff_holdings_vs_weights`; re-run
`packages/core/tests/test_curated_versions.py`. Persisted versions/posts unchanged.

## SC3 — Plans are preview-only with synthetic desk_plan_id · ⚠ UNREVIEWED

**Context.** docs/smallcase/02 Track C + PACK.2: the web app may generate invest/apply/exit
plans but must never execute. Desk non-negotiable #1: plans expire in 30 minutes. Leaf 1.2.2
owns plan generation; the versions sibling owns publish/diff.

**Taken.** Pure builders in `baskfy_core.curated_plans` (`build_invest_plan` / `build_apply_plan` /
`build_exit_plan`) produce desk-shaped legs + `expires_at_hint = now + 30m`. API
`POST /api/v1/cb/plans/{invest,apply,exit}` stamps synthetic `desk_plan_id = cb-sim-{uuid}` and
status `PLANNED` while the cash session is open. Weights/prices travel in the request body for
this leaf (no live quote fetch). **No `/execute`, no `OrderGateway`, no `place_order`.**

**Rejected.** Calling the live desk `/analyze` from the web API (couples Track A to desk
credentials and blurs the hand-off). Persisting `cb_order_batch` rows in this leaf (versions +
accounting siblings own ledger writes). Returning 4xx when the market is closed (UI needs the
next-open payload for `MarketClosedModal`).

**Reversal.** Delete `curated_plans` core + router modules and the `include_router` line in
`app.py`; OpenAPI loses the three preview routes. Swap synthetic ids for real desk plan ids only
after a written D3 path exists.

## SC3 — Market-hours guard is pure + calendar-injected · ⚠ UNREVIEWED

**Context.** docs/smallcase/04 §5: plan generation outside NSE 09:15–15:30 IST on a trading day
returns the closed-market response with next open — never a plan.

**Taken.** `baskfy_core.market_hours_cb` is clock-pure (`now` + `trading_dates` in). Session
bounds inclusive. `closed_market_payload` → `{market_open: false, next_open_ist}`. The plans
router loads NSE dates from `trading_day` and short-circuits to that payload before any builder
runs.

**Rejected.** Reading wall-clock inside core (breaks house rule 1). Hard-coding a holiday list
inside the hours module (calendar already lives in `trading_day` / trading_calendar). Blocking
with HTTP 403 (UI needs structured next-open for notify-me).

**Reversal.** Replace the guard call sites; keep the pure module for other surfaces (SIP, SC6
handoff).

## SC7 — SIP REMINDER calendar is pure; AUTO refused on write · ⚠ UNREVIEWED

**Context.** SC7 AC: holiday-aware next_fire, idempotent fire per (plan, month), no order path;
AUTO must stay off the write path until D3.

**Taken.** `baskfy_core.curated_sip`: caller injects `as_of` + `trading_dates`; nominal
`day_of_month` (1–28) snaps forward to the next trading day; `fire_idempotency_key` =
`{plan_id}:{YYYY-MM}`; `raise_sip_due` returns a pure `SIP_DUE` pending-action dict or
`None` when the key was already consumed; `assert_reminder_mode` refuses `AUTO` and any
non-REMINDER mode. Beat/DB wire deferred — pure evaluate helper is ready for the worker leaf.

**Rejected.** Embedding AUTO in the write enum "for later" (would fail the AC and the desk
non-negotiable). Reading wall-clock inside core. Calling OrderGateway from SIP fire.

**Reversal.** Delete `curated_sip.py` + tests; reintroduce AUTO only behind D3 and a separate
write guard.

## SC8 — Create UI saves PRIVATE locally with normalize; preview stubbed · ⚠ UNREVIEWED

**Context.** Gate leaf-1.6.2 only requires `/create/page.tsx`. Full E2E (persist + catalog
visibility) needs an API router the leaf marked optional.

**Taken.** Client form: ≥2 symbols, equal/custom weights via `lib/create/weights.ts`
(4 dp, residual on last, assert sum 1.0 mirroring `assert_weights_sum_to_one`); PRIVATE
framing on the page; preview copy stubbed; save is client-side confirmation until
`curated_create` API lands. No execute route.

**Rejected.** Shipping a half-wired create API that writes without visibility tests.
Instrument search against live `/instruments` in this leaf (adds API coupling; symbols typed
for now).

**Reversal.** Replace client save with POST to a create router; keep the weight helpers.

## SC6 — Investor routes empty until ledger APIs; fees in Account · ⚠ UNREVIEWED

**Context.** 05-ui-spec maps `/investments`, `/watchlist`, `/fees`. Watchlist list API exists
(`GET /api/v1/watchlist`); investments and fee-ledger list endpoints do not yet (SC4 pure math
only). Nav must stay honest; empty states beat invented fixture ledgers that look live.

**Taken.** Pages call `lib/investments/fetch` which prefers real GETs and returns empty shapes
on miss/error. Fees sit in **Account** (next to Invoices) with accrued-not-collected framing +
04 §1 FAQ. Investments + Watchlist sit in the primary nav after Explore. Order-shaped CTAs use
existing `PlanHandoffPanel` / `MarketClosedModal` via `InvestmentActions`. ShowDetailsModal
labels match SC4 `InvestorSnapshot` fields. Grep/vitest read-only suite extended to new routes.

**Rejected.** Shipping hard-coded mock investment rows as if they were live (misleading money
UI). Putting `/fees` in primary (it is account math, not a daily destination). Adding a web
`/execute` or form post for Invest more / Exit / Rebalance.

**Reversal.** Point fetch helpers at real `/cb/investments` and `/cb/fees` when those routers
land; remove empty-state copy. Move fees into primary if product IA changes.

## SC9 — Engagement API is sole-tenant list + dismiss/resolve; no orders · ⚠ UNREVIEWED

**Context.** SC9 AC wants updates + pending actions for the home/investments slots. Publish
already writes `CbUpdatePost` + `CbPendingAction` in `curated_versions`; the missing piece was
the read/mutate API.

**Taken.** `curated_engage` router: `GET /cb/pending-actions`, `GET /cb/updates`,
`POST …/dismiss`, `POST …/resolve`, scoped via `scoped_sole_user_id`. Thin
`PendingActionCard` on `/investments`. No execute paths.

**Rejected.** Building trending/collections/unread-dot in this leaf (broader SC9; can land
later without blocking the engage gate). Persisting dismiss in a separate notifications table
(model already has `dismissed_at` / `resolved_at`).

**Reversal.** Remove `curated_engage` include + router; investments falls back to inline card
markup; publish side-effects stay.

## SC10 — Track B flags default off; dark routes 404; Free Access while off · ⚠ UNREVIEWED

**Context.** docs/smallcase/02: subscriptions / fee collection / public signup stay dark until
D3. Gate leaf-1.7 only requires the subscriptions flag default false; the pack names all three.

**Taken.** `subscriptions_enabled`, `fee_collection_enabled`, `public_signup_enabled` default
`False` in settings. Mounted dark routes (`/cb/paywall`, `/cb/public-signup`, `/cb/fees/collect`)
raise `not_found` while off. `track_b.free_access` treats every basket as Free Access while
subscriptions are off (FEE labels may still say FEE).

**Rejected.** Gating existing `/auth/register` (would break sole-user auth). Leaving paywall
routes unmounted (404-when-off is the documented shape and keeps OpenAPI honest about the
surface name).

**Reversal.** Delete `track_b` router + helpers; keep flag fields at False until a D3 flip.

## Tree2 / 2.3 — Holdings windows are closed intervals · ⚠ UNREVIEWED

**Context.** Leaf 2.3 derives `cb_dividend` from holdings history
`{instrument_id: [(from_date, to_date, qty), …]}` × cash CA. The pack does not say whether
`to_date` is exclusive.

**Taken.** Closed `[from_date, to_date]`: qty on ex-date sums every window with
`from_date ≤ ex_date ≤ to_date`. Matches “qty held on ex-date” wording and NSE seller-on-ex
entitlement (seller still gets the cash). `total = money(amount_per_share × qty_held)`.

**Rejected.** Half-open `[from, to)` (would drop a sell-on-ex lot unless writers shift
`to_date` by one day). Inventing ₹ amounts from RECOVERED-ACTIONS factors (factors are not
₹/share; tests pin ex-dates from that file and supply cash fixtures).

**Reversal.** Flip `qty_held_on` to half-open and rebaseline the sold-before / bought-on-ex
tests; writers adjust window ends.

## Tree2 / 2.4 — Chart uses visx stubs until series API · ⚠ UNREVIEWED

**Context.** SC5 asked for SIP + benchmark on the detail page; catalog does not yet serve a
chain-linked EOD series.

**Taken.** Client `PerformanceChart` with real controls (range pills, SIP, benchmark) and
`series` props; empty series falls back to `stubPerformanceSeries(range)` so the SVG is not
blank. DisclosureBlock stays beside the chart. No order CTAs on the chart.

**Rejected.** Blocking on a metrics time-series endpoint (other Tree-2 leaves own API). Adding
a second chart library (docs/02 locks visx).

**Reversal.** Pass real points from fetch once the explore basket payload grows a series;
delete stub path when fixtures always have ≥2 points.

## Audit / 1 — a basket has no returns before its first version existed · ⚠ UNREVIEWED

**Context.** Fixing A1 (one version's weights replayed across all history) made the NAV series
version-aware: each day uses the weights of the version effective that day, and the series starts
on the first priced day at or after the earliest version's `effective_date`.

**Consequence, which is visible to a user.** A basket now reports `NULL` returns for any window
that reaches back before it existed. The SCAN-seeded `momentum-scan` basket has a single GENESIS
version effective at seed time, so its card shows no returns at all until history accrues.

**Choice taken.** Ship the empty card. A basket that did not exist has no track record, and the
alternative — a number computed from a portfolio nobody held — is the defect that was just fixed,
reintroduced one layer down. House rule 5 says no look-ahead, ever, and this is what that costs.

**Rejected.** (a) Backfill a synthetic version at the basket's `launched_at` — invents holdings
nobody chose. (b) Keep the old whole-window replay for SCAN baskets only — the same lie, scoped.
(c) Show the number with a caveat — a caveat does not make a backtest a record.

**How to reverse.** Seed backdated versions from the strategy's real historical scan output, which
is a genuine record because the scan actually produced those constituents on those dates. That is
work for the SCAN seeder, not for the metrics job, and it is the honest version of what (a) was
trying to do.

## Audit / 2 — the disclosure is a component on the surface, not a field on the card · ⚠ UNREVIEWED

**Context.** Every return the catalog publishes is a price return: `ohlcv_daily.close` carries
splits and bonuses, not cash dividends (`DECISIONS-MERGE.md` M39.3). CLAUDE.md now states the
rule — any new surface showing a return owes the reader the same sentence.

**Choice taken.** `MetricsOut` carries `return_convention`, `dividends_included` and
`return_convention_note`, and the web renders one `ReturnConventionNote` per surface, beside the
existing `DisclosureBlock`.

**Rejected.** One note per card. Repeated across twenty cards it becomes wallpaper, and wallpaper
is not a disclosure — it is the shape of a disclosure with none of the function.

**Also.** `dividends_included` is a real boolean, not the string `"false"` it was first written
as. Every non-empty string is truthy in JavaScript, so a UI writing the natural
`if (metrics.dividends_included)` would have rendered "dividends included" on a series that
excludes them: a disclosure asserting the opposite of the truth.

## Audit / 3 — S1 resolved in favour of M43.4, not the audit branch · ⚠ UNREVIEWED

**Context.** Two fixes for the same defect landed independently. `M43.4` **refuses** a principal
that is not the sole tenant; the audit branch **scoped to the caller**, returning the caller's own
id so a second account simply owns no rows.

**Choice taken.** `M43.4`. It shipped first, and it reads Law 2's multi-tenant clause literally —
the gateway refuses a mismatch. The audit branch dropped its version and rewrote its tests to
assert M43.4's contract.

**Residual, recorded rather than left implicit.** `M43.4` guards on
`principal_user_id is not None`, so a principal carrying no user id still receives the sole
tenant. Nothing reaches it that way because every handler on the router calls
`principal.require_user()` first. A test ties the two together, so removing the `require_user`
calls fails loudly instead of silently reopening the hole.

## Audit / 4 — `POST /cb/baskets` is a deliberate exemption from the read-only basket rule · ⚠ UNREVIEWED

**Context.** `test_baskets_readonly.py::test_every_basket_route_is_a_get` guards M22's rule that
the basket surface is read-only — "execution stays in the desk console, so nothing here crosses
the SEBI gate". SC8 added `POST /api/v1/cb/baskets` (create a PRIVATE basket) and the invariant
went red and stayed red through SC9–SC12 and the final report.

**Choice taken.** The route belongs; the test's list was widened **deliberately**, not
re-baselined. `02-scope-and-gating.md` puts the create/customize builder in **Track A — build
now, fully live**, and says the no-order-route test "stays green and is extended to the new
routes". Extending it is what was required. The gate this file guards is *execution*, not the
HTTP verb: the route writes one `cb_basket` row plus its GENESIS version and constituents for
the sole tenant, and moves no shares, no money and no broker state.

**Why this is not a silent widening.** A named `DELIBERATE_MUTATING_BASKET_ROUTES` table carries
the exemption, and two new tests make the invariant *stronger* than before it went red: one fails
if an exemption names a route that no longer exists, so a stale hole cannot linger unwatched; the
other fails if the exempted router can reach `baskfy_execution`, `OrderGateway`, `place_order` or
`kiteconnect`, so the exemption is only valid while the route genuinely cannot execute. Any other
mutating basket route still fails the original assertion.

**Rejected.** (a) Leaving it red — an invariant nobody can distinguish from a real breach stops
being an invariant. (b) Deleting the assertion — the same, with the evidence removed.

**How to reverse.** Remove the entry from `DELIBERATE_MUTATING_BASKET_ROUTES` and the route fails
the original assertion again.
