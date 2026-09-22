# OP run — live status

The status page for the options run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: OP0 🟡, OP1 ✅, OP2 ✅, OP3 🟡, OP4 ✅, OP5 ✅ (22 Sep 2026); OP6 not started.** Pack written 22 Sep 2026 on branch
`developer`, absorbing the never-started condor pack (`docs/condor/`) as sleeve O1. OP0-OP5 were each run alone, by instruction ("execute only OP<N>, then stop"); the next session
resumes at OP6 (and the orchestrator feeds back OP3's box probe — §"What is NOT done" under OP3).

## Module ledger

| Module | State | One line |
|---|---|---|
| OP0 — Baseline, read-in, verified facts | 🟡 | Costs, expiry circular, F&O segment, algo rules, `OPTIONS_ENABLED` blast radius verified; six live Kite reads pending a session; full suites not re-run (memory rule) |
| OP1 — The shared pure core `baskfy_core.options` | ✅ | 11 modules, 635 tests against `04`; purity, mypy strict, ruff, escape hatches clean; mutation 89.6 % (499/557), every survivor justified |
| OP2 — Schema, NFO master, settings, `options_gates()` | ✅ | `0050_options` (17 tables, `op_sleeve` enum, monthly chain partitions), nightly NFO master + `op_expiry`, verified event-day seed, 9 flags + 6 ceilings in desk/API/worker, `options_gates()`, `OptionsSettings`; gateway product gate tightened (OP0.6); 587 new tests |
| OP3 — Provider reads, index minute bars, collector, limiter | 🟡 | Option quotes with depth/OI, minute bars, basket margins; collector + index-bar tasks; per-family shared limiter proven on Redis. **Probe run on the box 22 Sep 2026 after deploying `1c9a3a5`: five of six reads answered** (evidence `docs/options/evidence/op3-probe-2026-09-22.json`); (b) waits for an expiry to pass. **Collector and scan flags ON on the box since 22 Sep 21:4x** (OP3.11). Backfill not run; live limiter share not measured |
| OP4 — Sleeve signal cores and the scans | ✅ | `bars`, `structures`, `condor`, `directional`, `expiry_setups`, `scan` (pure; each sleeve's day function, `build`, exits); task `baskfy.options.scan` behind the scan **and** collect flags (both false), DB-only, idempotent per minute; 167 new tests; no live data yet (collector off) |
| OP5 — API and the web Options tab (the scans ship) | ✅ | `routers/options.py` (10 paths, 2 money-free mutations, everything else 405), `/options`, `/options/journal`, `/options/calendar`, `/me/options`; Options appended after Tight; honest empty state names the switch; SEBI caveat + "Scan · paper only" on every page; 30 API + 34 web tests; e2e spec written, not run |
| OP6 — O1 plan builder (monthly + weekly), costs pinned | ⬜ | |
| OP7 — O2 plan builder | ⬜ | |
| OP8 — O3 plan builder | ⬜ | |
| OP9 — Desk process `options_monitor` | ⬜ | |
| OP10 — Desk page + `/nifty-options/execute` (paper) | ⬜ | |
| OP11 — Journal, ledger, pauses, first-live multiplier | ⬜ | |
| OP12 — Backtests: Tier 1–2 per sleeve; Tier 3 ⛁ | ⬜ | |
| OP13 — Gating and safety proof | ⬜ | |
| OP14 — Hardening and observability | ⬜ | |
| OP15 — Verification, goldens, deploy, final report | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial · ⛁ data-blocked.

## Paper periods (`02` §3.2) — start only after OP15 deploys

| Sleeve | Needed | Done | Traded |
|---|---|---|---|
| O1-M | 6 monthly expiries, ≥ 3 traded | 0 | 0 |
| O1-W | 12 weekly expiries, ≥ 6 traded | 0 | 0 |
| O2 | 60 sessions, ≥ 25 traded | 0 | 0 |
| O3 | 20 expiry days, ≥ 8 traded | 0 | 0 |

---

## OP0 — Baseline, read-in, verified facts

**🟡, 22 Sep 2026.** Everything a fresh session needs to stand on is below. Why not ✅: the six
live Kite reads of `06` OP0 (§4 here) need a Kite session this run did not have and was told not to
open, and the two full suites were not re-run (the Mac's memory rule; OP0 changed no code). Neither
blocks OP1, which is pure arithmetic over fixtures. Decisions: `DECISIONS-OP.md` OP0.1–OP0.9.

### 1. Where the repo stands

| Item | Value |
|---|---|
| Branch / HEAD at start | `developer` / `6f16214` ("TWT: auto-execute is on on the box…") |
| Others' dirty files (never committed under an OP module) | `holdings-status/` (untracked) |
| Alembic head | `0049_broker_trade` (50 revision files, one head; `decile-blueprint/services/api/alembic/versions`) — computed from the files, no database touched |
| Beat inventory | 44 entries in `baskfy_worker/celery_app.py` `BEAT_SCHEDULE` (l.108): capture-kite-trades, bhavcopy-eod, refresh-reference-data, session-catch-up, purge-deleted-accounts, weekly-integrity-audit, reap-abandoned-pipeline-runs, publish-deadline-slo, kite-token-expiry, queue-backlog, dispatch-screen-alerts, sweep-webhook-deliveries, cb-eod-metrics, cb-sip-reminders, cb-dividends, cb-sync-batches, portfolio-eod-nav, swing-eod, swing-eod-plan, vbt-detect, twt-detect, twt-evening, twt-morning, vbt-evening, vbt-morning, swing-weekend, swing-premarket-levels, swing-premarket-gaps, swing-timing-probe, swing-check-monitor-started, swing-check-orders-after-cutoff, swing-check-gtt-at-1515, swing-check-detect-fresh, vbt-rescan-sweep, twt-scan-publish, vbt-check-detect-fresh, vbt-check-orders-past-expiry, vbt-check-naked-positions, vbt-check-positions-without-bars, swing-catalyst, swing-scan-sweep, kite-login-nudge, kite-login-nudge-second, cb-rebalance-notify. **No options entry exists**; OP3 adds the collector/scan/master jobs |
| Kite token state | Dev Mac: **no session** (`kite-momentum-rebalancer/data/.kite_token.json` absent; only its `.key`). The box's token was not inspected (no deploy, no ssh in OP0) |
| Suites | Desk: **2,123 collected**; screener tree (`decile-blueprint`, `uv run pytest`): **9,071 collected**. Run green in OP0: desk `test_frozen_boundary`, `test_guards`, `test_settings_boundary`, `test_seven_non_negotiables` — **143 passed, 12 subtests**; `packages/execution/tests/test_non_negotiables.py` — **25 passed**. Full runs not repeated (OP0.9); run `tools/ci-local.sh` before OP1's first code change |
| Route collision | Web app has **no** `/options` route (`apps/web/src/app/(app)` lists none) — free for the Options tab. Desk `/options*` is the frozen lab's hook (§5) — hence `/nifty-options` (PACK.9), which is free |

### 2. The flags, in every env file (values only; no secret was printed)

| File | `DRY_RUN` | `INTRADAY_ENABLED` | `OPTIONS_ENABLED` |
|---|---|---|---|
| `.env.example` | `true` (l.427; `BASKFY_DRY_RUN=` empty override l.68) | `false` (l.472) | `false` (l.473) |
| `.env.staging` | `true` (l.90, l.147) | `false` (l.96) | `false` (l.97) |
| `.env.staging.example` | `true` | absent → code default `false` | absent → `false` |
| `.env.staging.compose` | absent | absent | absent |
| `kite-momentum-rebalancer/.env` | `true` | absent → `false` | absent → `false` |
| `kite-momentum-rebalancer/.env.example` | `true` | `false` | `false` |
| `decile-blueprint/.env`, `.env.example`, `infra/docker/.env`, `apps/web/.env.local` | absent | absent | absent |
| `infra/docker/compose.prod.yml` (desk service) | default `"true"` | `"false"` literal (l.436) | `"false"` literal (l.437) |
| `.github/workflows/ci.yml` | — | — | `"false"` (l.228) |

Code defaults: `app/config.py:116–117` → both `false` unless the env says exactly `true`. The desk's
`LOCKED_KEYS` (`app/analytics/settings.py:42`) = `DRY_RUN`, `INTRADAY_ENABLED`, `OPTIONS_ENABLED`, …
(env-only, never a form field; `tests/test_settings_boundary.py:67` asserts the three). No
`BASKFY_OPTIONS_*` variable exists yet; `.env.example`'s stale `BASKFY_CONDOR_*` block is read by
nothing (OP0.7).

### 3. Facts verified from primary sources (URLs and quotes in OP0.1, OP0.2, OP0.8)

| Fact | Verified value | Source |
|---|---|---|
| STT, option sale | **0.15 % of premium** from 1 Apr 2026 (was 0.1 %) | Finance Bill 2026 Memorandum, Clause 143; Zerodha charges |
| STT, option exercised | **0.15 % of intrinsic** from 1 Apr 2026 (was 0.125 %) | same |
| NSE options transaction charge | **0.03553 %** of premium, from 1 Mar 2026 | Zerodha charges; NSE/FA/73061 |
| NSE IPFT, options | **₹0.01 / crore** of premium + GST | Zerodha charges; NSE/FA/73061 |
| Brokerage / SEBI / stamp / GST | ₹20 per order / ₹10 per crore / 0.003 % buy / 18 % on brokerage+SEBI+txn | Zerodha charges |
| Zerodha MIS auto square-off, F&O | **15:26**; ₹50 + GST per order | Zerodha support article |
| NIFTY expiry day | weekly **Tuesday**, monthly **last Tuesday**, for contracts expiring on/after **1 Sep 2025** | NSE/FAOP/68747 (25 Jun 2025) |
| Retail-algo rules (from 1 Apr 2026) | static IP mandatory; < 10 orders/s needs no strategy registration; API orders tagged; MARKET orders need non-zero market protection | Zerodha's "In the Money" write-up |
| **F&O segment active** | **Yes** — filled NFO MIS orders on 21 Sep 2026 (placed by hand as a Kite basket, not by Baskfy); Maulik's "yes" 22 Sep 2026 | coordinator's read-only `get_orders` (OP0.4) |

`04` §6 now carries these rates. **Not verified, stays as written:** the retail-algo question of who
stamps the algo ID (NEEDS-MAULIK § Options O4).

### 4. Live Kite reads — **pending a Kite session** (OP0.3; nothing guessed)

| # | Call (read-only, through the limiter) | Records |
|---|---|---|
| (a) | `historical_data(<NIFTY 50 token 256265>, …, "minute")` and INDIA VIX (`264969`), probing back by year | earliest date served (`07` §1 says ~2015 — unverified) |
| (b) | `instruments("NFO")` for last Tuesday's ATM NIFTY CE; `historical_data` on its old token | absent from master; the error text |
| (c) | `instruments("NFO")`, NIFTY CE/PE, next eight expiries | each expiry's weekday, `lot_size`, `tick_size`, modal strike step near ATM, which is monthly (the last in its month) |
| (d) | `margins()` | superseded by OP0.4 — segment active; OP3 still records the NFO margin fields' shape |
| (e) | `quote()` on five NIFTY options | fields, depth levels, **OI unit** (contracts or shares — `04` §2.4 depends on it), `oi_day_high/low`, timestamps |
| (f) | `basket_order_margins` for a fixture four-leg condor (no order) | answers / shape |

Known from code and Kite Connect docs, not live-verified: dump columns `instrument_token,
exchange_token, tradingsymbol, name, last_price, expiry, strike, tick_size, lot_size, instrument_type,
segment, exchange`; `quote()` ≤ 500 instruments a call (`baskfy_providers/kite.py:76`); five depth
levels a side; minute history ≤ 60 days per request. Corroborated by the 21 Sep order book: NIFTY
weekly trading symbols are `NIFTY<YY><M><DD><strike><CE|PE>` and 50-point strikes are listed near ATM.
The provider today has `instruments()`, `historical_data()` (day only, `_fetch_chunk` l.462),
`margins()`, `quote()` via `quotes()` (l.411; `QuoteRecord` carries no depth or OI — OP3 extends it);
`basket_order_margins` is called only by the desk (`app/main.py:298`). **Lot size: read from the
master, never recorded here as a number.**

### 5. What `OPTIONS_ENABLED=true` would wake today (read-only inventory; OP0.5, OP0.6)

| # | Where | What happens with the flag true | Money? |
|---|---|---|---|
| 1 | `packages/execution/.../guards.py:91–94` `product_exchange_refusal` via `gateway.py:248` (orders) and `:510` (GTT) | **Every** product on NFO/BFO/CDS/BCD/MCX passes the product gate — including **MIS without `INTRADAY_ENABLED`** and **NRML futures**; only options under NRML/CNC are refused (`assert_not_overnight_option`, `guards.py:55`) | **Yes — the real exposure (OP0.6)** |
| 2 | `app/core/gateway.py:24–30` `_gates_from_config` | The desk's default gateway (the weekly rebalancer's) inherits `options_enabled=True`. Swing (`swing_execute.py:412`), TWT (`twt_execute.py:146`), VBT (`vbt_execute.py:82`) pin `False` and are unaffected | via #1 |
| 3 | `app/main.py:691–700` `_options_view`; routes `/options` (l.704), `/options/run` (l.721), `/options/data` (l.746) | Pass the flag check, then `import analytics.options_view` fails (frozen) → **404**, logged warning | No |
| 4 | `app/templates/base.html:38` (+ `main.py:77` injects `options_enabled`) | Nav gains an **Options** link to that 404 | No |
| 5 | `app/analytics/ops.py:139–164` `_options_operations()` → `all_operations()` l.344, `by_name()` l.354; consumed by `/ops` (`main.py:992`, `:1007`) | **Five strangle operations appear on `/ops`** — the `ImportError` guard targets `strategies.strangle.instruments`, which is live, so it never fires; starting one spawns `python -m scripts.strangle` (frozen) → `ModuleNotFoundError` | No (paper lab, code absent) |
| 6 | `app/analytics/autorun.py:137–147` → `_underlyings` l.150 (lists nifty/banknifty/sensex: `config/strangle*.yaml` exist) → `_options_items` l.168 | After 09:15, `_observed_today` (l.36) imports `strangle.calibrate` (frozen) → caught `ImportError`, nothing offered; before 09:15 the entry-window veto returns nothing. **Dark by accident, not design** | No |
| 7 | `app/strategies/strangle/{__init__,calendar_nse,clock,config,instruments}.py` (614 lines, live) | Imported unconditionally by `scripts/autorun.py:25–27` (the login autorun, `main.py:271`) whatever the flag; the flag only decides whether they are *used* by #5/#6 | No |
| 8 | Tests reading the flag: desk `test_pages.py:256`, `test_regime_view_backtest.py:444,459`, `test_guards.py:127`, `test_frozen_boundary.py`; CI pins it false (`tools/ci-local.sh:53`, `ci.yml:228`) | Would change which pages those tests request | — |
| 9 | Screener tree (`services/api`, `services/worker`, `apps/web`) | Reads nothing — `kite_basket.py:63` only mentions it in a comment | — |

**Verdict:** flipping `OPTIONS_ENABLED` today widens the weekly desk's gateway beyond options-MIS
(#1) and shows five dead controls on `/ops` (#5). OP2 should tighten #1 (OP0.6); OP13's side-door
test must cover #3–#6 and gate them on a *frozen* module's presence (OP0.5). `02` §3 cannot be met
before both.

### 6. The frozen lab's observations (read-only, this Mac)

`kite-momentum-rebalancer/data/outputs/`: `strangle_{nifty,banknifty,sensex}_journal.jsonl` (7 / 4 /
3 rows, 18–19 Aug 2026: `collected` straddles and `skipped` verdicts — "secondary broker not
authenticated"), `_straddle_record.jsonl` (2 / 1 / 1 rows: session, expiry, dte, spot, ATM strike,
call, put, straddle, straddle % of spot), `_lockout.json` (19 Aug, SKIPPED), an empty
`strangle_commitments.json.lock`. **No `options_forward.jsonl`** anywhere. NIFTY has two ATM
straddle observations (19 Aug 2026, 4 DTE) — too few for any Tier 2 check; `07` §1's "at best a
partial check" is, on this machine, no check. The box's collectors (NEEDS-MAULIK #2) may hold more;
not inspected. Incidental corroboration: the journal shows NIFTY expiries 18 and 25 Aug 2026
(Tuesdays) and SENSEX 20 Aug (Thursday).

### 7. NEEDS-MAULIK

`## Options` opened (O1–O6) from `QUESTIONS.md`'s hands-only items; the Condor section is marked
superseded. The F&O-segment question is **not** there — it is answered (OP0.4).

### What blocks OP1

Nothing (OP1 is now ✅). OP2 should carry OP0.6's gateway tightening and OP0.7's env rename; OP3
does §4's six reads first.


## OP1 — The shared pure core `baskfy_core.options`

**✅, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP1.1–OP1.11.

### What exists (`decile-blueprint/packages/core/src/baskfy_core/options/`)

| Module | `04` | What it holds |
|---|---|---|
| `config.py` | all | `OptionsConfig` — ten frozen groups (`calendar`, `chain`, `condor_monthly`, `condor_weekly` — distinct instances, `directional`, `expiry_setups`, `costs` = `CostRates` with OP0's verified rates and their URLs, `sizing`, `execution`, `risk`), 147 fields; `Sleeve`/`SleeveGroup`/enums; `OptionsCeilings` (the `02` env ceilings' defaults, not config); NIFTY-only refusal |
| `calendar.py` | §1 | `Contract` (a master row), `expiries`, `monthly_expiry`, `kind`, `role` (§1.2's table), `next_session`, `lot_size_for`/`tick_size_for` (`None` when missing/0/ambiguous), `expiry_for_o2`, `expiry_for_o1_o3` — no weekday arithmetic (source-scanned) |
| `greeks.py` | §2.3 | `year_fraction` (calendar minutes), Black-76 price/greeks on `F`, bisection IV on [0.01, 5.0] to 1e-6, `solve` with the three refusals |
| `chain.py` | §2.1–2.5 | `OptionQuote`/`Level`, `atm` (ties low), `strike_step` (modal, read), `snapshot_strikes`, `parity_forward` (with fallback), `quote_greeks`, `is_liquid` (every reason; wing = depth only), OI unit conversion, the three staleness tests |
| `costs.py` | §6 | `charges` (per component, to the paisa), `exercise_stt`, `settlement_stt` (the STT trap), `synthetic_half_spread`, the three expected gains, `cost_test`, `rates_review_due` |
| `sizing.py` | §7, §4.6, §5.5 | risk per lot for condor/long/debit spread, `gap_through_long`, `size` (paper-one-lot, `NO_SLEEVE_CAPITAL`, ceilings, first-live ×0.5), `premium_cap_ok`, `margin_check` |
| `session.py` | §11 | the seven-edge machine, `transition` (raises), one session per sleeve per date, `plan_expires_at` |
| `risk.py` | §9 | `evaluate_sleeve` (DAILY/WEEKLY/MONTHLY_R), `trade_breached`, `book_limits` (derived, capped), `evaluate_book`, `plan_refusal` → `REJECTED_PAUSED` |
| `journal.py` | §12 | `summarize` per `(sleeve, simulated, sizing_mode)`, `summarize_one` refuses pooling; R, win rate, expectancy ₹/R, worst R, drawdown ₹/R, MAE/MFE, by reason / expiry kind / weekday |
| `execution.py` | §8 | entry/exit sequences (naked short refused), `advance_entry`, `next_exit_leg`, `never_naked`, `next_attempt` (1, reprice, cancel; marketable LIMIT for a risk-reducing third), `simulate_fill` (depth ladder, no LTP), precedence, stale-mark filter, `feed_lost`, the expiry-day slot |
| `backtest.py` | §13 | the engine: one sleeve, one tier, Tier 1 without P&L, caveats verbatim from `07`, Tier 3 banner, `tier2_quote` |

Ported by re-implementation from the frozen lab's `options.py`, `options_costs.py`,
`strangle/fills_paper.py`, `strangle/calendar_nse.py` (named in each docstring); nothing imports
`frozen/` (purity test). `04` gained **§14**, the machine-checked field table (OP1.2).

### Tests (`packages/core/tests/test_options_*.py`, `options_fixtures.py`)

| File | Tests |
|---|---|
| `test_options_calendar.py` (holiday shift defeats a weekday rule; O1-W refuses the monthly Tuesday) | 128 |
| `test_options_greeks.py` (Hull's Black-76 example; parity to 1e-9; delta parity; finite differences; IV round trips; refusals; forward; liquidity; staleness) | 52 |
| `test_options_costs.py` (worked round trip to the paisa; each rate a field; the STT trap both ways) | 24 |
| `test_options_sizing.py` | 23 |
| `test_options_session.py` (all 49 pairs; 500 random edge walks) | 60 |
| `test_options_risk.py` | 19 |
| `test_options_journal.py` (never pools) | 13 |
| `test_options_execution.py` (never-naked over 500 seeded fill sequences per structure, entry and exit) | 42 |
| `test_options_backtest.py` | 12 |
| `test_options_purity.py` | 28 |
| `test_options_docs_parity.py` (§14 both ways, prose numbers, variants distinct, NIFTY only) | 166 |
| `test_options_edges.py` (the boundaries the first mutation run found unasserted; every result frozen) | 68 |
| **Total new** | **635** |

**Suite counts.** Before OP1's first change (collected): screener core `packages/core/tests` **5,173**,
screener tree **9,071**, desk **2,123**. After: core **5,820**, tree **9,718**, desk **2,123**
(635 new options tests, plus 12 existing file-parametrised tests that pick up the twelve new
source files; the desk is unchanged — OP1 touched no desk file). `tools/ci-local.sh` before the first change: **15 passed, 2 failed, 3 skipped** —
the failures were "Tests, with per-package coverage gates" and "Performance budgets" (the latter a
latency budget on a loaded Mac). After: **16 passed, 1 failed, 3 skipped** — desk suite, lint and
type-check, perf budgets all PASS; the one failure is the same coverage-gate step, failing on three
tests that do not touch options and fail without it: `services/api/tests/test_api_admin.py::
TestUserLookupAndOverrides::test_an_override_changes_the_effective_entitlements` (fails in
isolation too) and two `packages/providers` Kite-health tests that pass in isolation and fail only
in the full run (environment/order). **Not OP1's to fix; recorded here so the next module does not
mistake them for its own.**

**Lint.** `ruff check`, `ruff format --check`, `mypy --strict` clean on the package, its tests and
`tools/mutation.py`; `test_no_escape_hatches.py` green.

**Mutation.** `tools/mutation.py` gained `OPTIONS_TARGETS` (ten modules; `config.py` excluded as
swing's is). First run 74.5 % (415/557) exposed real gaps, closed by
`test_options_edges.py`; second run **89.6 % (499/557)**, all 58 survivors justified in
`reconciliation/mutant-justifications.json` (swing's last: 88.2 %). Report: `decile-blueprint/reconciliation/MUTANTS-options.md`.

### What is NOT done (by design, for later modules)

* The sleeves' signal/structure/exit modules and the scan (`condor`, `directional`,
  `expiry_setups`, `scan`) — **OP4**. The engine's day functions, tools and worker task — OP12.
* `chain.oi_unit` defaults to `UNITS` **unverified** (OP1.3) — OP3's first NFO `quote()` confirms it.
* Nothing is wired to a database, the desk or the web; no flag exists yet (OP2).

## OP2 — Schema, the NFO master, settings, `options_gates()`

**✅, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP2.1–OP2.10.

### What exists

| Piece | Where | Notes |
|---|---|---|
| Gateway product gate tightened (OP0.6) | `packages/execution/src/baskfy_execution/guards.py` `product_exchange_refusal` | Derivative venue: `OPTIONS_ENABLED` required, **MIS only**, and MIS needs `INTRADAY_ENABLED` there too. `assert_not_overnight_option` unchanged. Cash branch unchanged. OP2.1 |
| Migration **`0050_options`** (revises `0049_broker_trade`; single head) | `services/api/alembic/versions/0050_options.py` | 17 tables + enum `op_sleeve`; `op_chain_snapshot` RANGE-partitioned by month (Sep 2026 → Dec 2027, IST bounds, no DEFAULT — OP2.5); downgrade drops all, round trip tested on `baskfy_test` |
| Models | `packages/core/src/baskfy_core/models/options.py` | Vocabularies imported from `baskfy_core.options`; market tables shared, every other table `user_id` cascading |
| `options_gates()` (pure) | `packages/core/src/baskfy_core/options/gating.py` | `OptionsFlags`, `OptionsGate`, flag/ceiling env names; LIVE iff all four; the paper gates (OP2.3) |
| Desk readers | `kite-momentum-rebalancer/app/config.py` (13 new env reads), `app/options_gates.py` (`options_gates`, `product_gates`), `app/analytics/settings.py` `LOCKED_KEYS` (+13) | Nothing constructs an options gateway yet (OP10) |
| Worker | `baskfy_worker/options/` (`options_gates`, `options_ceilings`, `master.py`, `partitions.py`), `seeds/options_event_days.py`, `options_cli.py` (`seed`, `refresh-master`), task `baskfy.options.refresh_master` + Beat `options-contract-master` 19:30 mon–fri, route `baskfy.options.*` → default, alert `OPTIONS_MASTER_CHANGED` | OP2.7 |
| Provider read | `KiteProvider.option_contracts(underlying)`, `OptionContractRecord` | One `instruments("NFO")` call, dump columns only |
| API | `baskfy_api/options_settings.py` (patch models, ceilings, derived ₹ risk, audited writes), `settings.py` (7 flags + 6 ceilings), problem type `underlying-not-allowed` (422) | Routes are OP5's |
| Env | root, `decile-blueprint` and desk `.env.example`: `BASKFY_OPTIONS_*` block, all flags `false`; `BASKFY_CONDOR_*` removed (OP0.7) | |
| Event days seeded (source `SEED`, URL on the row) | RBI MPC decision days FY 2026-27: 8 Apr, 5 Jun, 5 Aug, 7 Oct, 4 Dec 2026, 5 Feb 2027 | Budget 2027-28 not announced → not seeded (OP2.6) |

### AC → test

| `06` OP2 AC | Test |
|---|---|
| migrate → seed → migrate is a no-op | `packages/core/tests/test_options_schema.py::TestMigrateSeedMigrate` (second `upgrade head` runs nothing; second seed writes 0 rows; a chosen number survives) |
| calendar's next monthly equals the master's; a shifted expiry moves it | `services/worker/tests/test_options_master.py::TestTheMaster::test_the_calendar_monthly_is_the_masters_and_a_holiday_shift_moves_it` (27 Oct → 26 Oct fixture) |
| every user-scoped `op_` table has `user_id` | `test_options_schema.py::test_every_user_scoped_table_has_a_cascading_non_null_user_id` (13 tables; 4 market tables have none) |
| `options_gates()` PAPER for all but all-four-true, 16 rows × every sleeve | core `test_options_gating.py` (5 sleeves × 16), worker `test_options_master.py` (5 × 16, desk-style parsing), desk `tests/test_options_gates.py` (5 × 16 + `product_gates`) |
| adding BANKNIFTY to any config is a 422 | `services/api/tests/test_options_settings.py::TestNiftyOnly` (book and sleeve patches; pure config raises; DB CHECK in `test_options_schema.py`) |
| M4.1 boundary intact | desk `tests/test_settings_boundary.py::TestTheOptionsBoundary` (13 keys locked, `OPTIONS_ENABLED`/`INTRADAY_ENABLED` locked, no `*AUTO*`), API ceilings + audited writes |
| Guard (OP0.6), tests first | `packages/execution/tests/test_derivative_product_gate.py` (174; red before the change, green after) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/execution/tests/test_derivative_product_gate.py` | 174 |
| `packages/core/tests/test_options_gating.py` | 115 |
| `packages/core/tests/test_options_schema.py` (db-marked half runs on `baskfy_test`) | 53 |
| `services/worker/tests/test_options_master.py` | 100 |
| `services/api/tests/test_options_settings.py` | 42 |
| desk `tests/test_options_gates.py` | 83 |
| desk `tests/test_settings_boundary.py::TestTheOptionsBoundary` | 19 |
| `packages/core/tests/test_schema_matches_docs.py::test_options_tables_are_recorded_in_docs` | 1 |
| **Total** | **587** (485 screener tree, 102 desk) |

**Suites.** Desk full run: **2,221 passed, 17 skipped, 12 subtests** (`DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false`). `packages/execution/tests`: **378 passed**. Swing/TWT/VBT/weekly guard files green. CI: see below.

**`tools/ci-local.sh` (one full run, at the end): 15 passed, 2 failed, 3 skipped** (the same 3 web
skips as OP1). The two failed steps, explained:

* *Tests, with per-package coverage gates* — the whole of both suites ran; **five** tests failed.
  Two were OP2's and are fixed and re-run green: `test_schema_matches_docs.py::
  test_no_undocumented_tables` (the 17 `op_` tables are now in its list, plus
  `test_options_tables_are_recorded_in_docs`) and `test_ops_and_alerts.py::
  test_the_runbook_map_is_complete` (new `docs/runbooks/10-options-master.md`, mapped for
  `OPTIONS_MASTER_CHANGED`). **Three are the pre-existing, non-options failures OP1 recorded**:
  `services/api/tests/test_api_admin.py::TestUserLookupAndOverrides::
  test_an_override_changes_the_effective_entitlements` (hard-coded 2026-09-21 expiry; still fails in
  isolation) and the two `packages/providers` Kite-health tests (`test_cli_doctor.py::
  TestWithoutCredentials::test_kite_is_reported_unavailable_with_the_reason`, `test_kite.py::
  TestHealth::test_unconfigured_is_unavailable_not_an_exception`), which pass in isolation. Not
  OP2's to fix; not weakened. The coverage-gate step was not re-run end to end (the Mac's memory
  rule: one full CI); the per-package percentages of this run were not captured.
* *Performance budgets* — failed **only because this session ran db-marked tests (including the
  schema round trip) concurrently with it**; re-run alone afterwards: **all 17 benchmark tests
  pass**. Lesson for OP3+: never run db tests while `ci-local.sh` is running.

### What is NOT done (by design, for later modules)

* **No route** reads or writes `op_*_config` yet (OP5); `OptionsSettings` is exercised by tests only.
* **Nothing has run against a real master**: no Kite session on this Mac (OP0.3). The first 19:30 run
  on the box (after deploy) is the first real `op_contract`/`op_expiry`; its columns follow Kite's
  documented dump. Lot size is still unrecorded as a number — it is read.
* `op_index_minute`, `op_chain_snapshot`, `op_scan` exist but nothing writes them (OP3/OP4); OP3's
  collector must call `ensure_chain_partition` for this month and next before writing.
* The `/ops` side door (OP0.5) is untouched — OP13.
* The seed is not part of `make seed`; run `uv run python -m baskfy_worker.options_cli seed` on the
  box after migrating (NEEDS no Maulik: it writes zero capitals and verified dates only).
* Not deployed, not pushed.

## OP3 — Provider reads, index minute bars, the collector, the limiter

**🟡, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP3.1–OP3.10. Why not ✅: the live half of `06`
OP3's AC needs a Kite session — the six reads (now a probe the orchestrator runs on the box), the
Tier-1 backfill on the dev stack, and a measured limiter share (⛁). Everything testable without
Kite is built and green.

### What exists

| Piece | Where | Notes |
|---|---|---|
| Provider reads | `packages/providers/src/baskfy_providers/kite.py`, `records.py` | `option_quotes(keys)` → `OptionQuoteRecord` (depth, OI, OI-day high/low, both timestamps; zero padding dropped; NFO + `NSE:NIFTY 50` in one call); `minute_bars(token, start, end)` in 60-day windows (`MINUTE_MAX_DAYS_PER_REQUEST`), aware IST; `basket_order_margins(legs)` (a calculation, `consider_positions=False`); `margins_shape()` (names/types only). OP3.1 |
| Per-family shared limiter | `packages/providers/src/baskfy_providers/factory.py` | `KiteFamily`, `kite_family_key`, `KITE_FAMILY_RATE_PER_SECOND` {quote 1, historical 3, general 9} = the desk's; `build_kite_family_limiter` = [bulk], `read`, family — the desk's keys and order (SW21/M85). OP3.5 |
| Worker adapters | `baskfy_worker/options/reads.py` | `build_options_kite()` → one adapter per family |
| Index minute bars | `baskfy_worker/options/index_bars.py` | intraday (since last stored minute), EOD reconcile, resumable Tier-1 backfill; closed minutes only; upsert on `(instrument_id, ts)`; index rows found by symbol, token from `instrument`. OP3.7 |
| Chain collector | `baskfy_worker/options/collector.py` | two nearest expiries × ±15 strikes × CE/PE (≤ 124) + spot = **one** `quote()` (≤ 125 keys; > 500 refused); parity forward + Black-76 IV/greeks at write, rounded; append-only `ON CONFLICT DO NOTHING`; ensures this month's and next month's partitions first; `collect_gate` = flag → 09:15–15:30 → NSE calendar → Kite session. OP3.4, OP3.9 |
| Celery tasks + Beat | `tasks/celery_tasks.py`, `celery_app.py` | `baskfy.options.collect_chain`, `.index_bars` (Beat every minute 09–15 mon–fri, `expires` 55 s, `acks_late=False`), `.index_bars_eod` (15:45), `.backfill_index_bars(from, to)` (on demand, bulk lane). OP3.3, OP3.8 |
| Probe (OP0 §4 a–f) | `baskfy_worker/options/probe.py`; `options_cli probe` | Read-only JSON report; each read independent; OI-unit verdict by divisibility; margins shape only. OP3.6 |
| CLI | `baskfy_worker/options_cli.py` | `probe`, `backfill-index-bars --from D [--to D]`, `collect-once` (ignores only the flag) |

**The collector's flag:** `BASKFY_OPTIONS_COLLECT_ENABLED`, default **false** (OP3.2); it also gates
the two index-bar tasks. Enabling it on the box is the orchestrator's decision after the probe.

**Run the probe on the box** (after the deploy and a Kite login that day):

```
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose \
  --env-file .env.staging.compose -f compose.prod.yml exec -T worker \
  python -m baskfy_worker.options_cli probe'
```

It prints JSON and writes nothing. Its answers replace OP0 §4's "pending" rows; (e)'s `oi_unit.verdict`
decides `chain.oi_unit` (OP1.3) — if `LOTS`, change the default and `04` §14 together. (b) will say
"no expired contract yet" until the nightly master has watched the **29 Sep 2026** expiry pass.

### The rate-limit proof (`packages/providers/tests/test_kite_options.py`)

* **Same clocks as the desk:** the options adapters' keys, rates and order equal the desk's
  `SHARED_KEY_PREFIX` / `RATE_FOR_FAMILY` / `DeskLimits.slot` (read from the desk's source) — so the
  collector, the desk page and the swing monitor queue on **one** 1 req/s quote clock and one 3 req/s
  historical clock under the one 3 req/s read ceiling.
* **Measured on Redis:** a desk-shaped caller and a collector-shaped caller, six calls each, on one
  departure clock (test keys at 10× Kite's rates): no two departures closer than the interval
  (−20 ms timer slack), the run's span ≥ 11 intervals.
* **Share (arithmetic, OP3.10):** ≤ 2 quote calls/min of 60 (3.3 %), 2 historical/min of 180
  (1.1 %), ≤ 4 of the 180/min read ceiling (2.2 %). The swing timing probe that `06` says to replay is
  still "NOT RUN YET" (`docs/swing/status/S2-kite-timing.md`), so no replay was possible.
* No Redis → the limiter is `None` and the adapter refuses every call.

### AC → test

| `06` OP3 AC | Test |
|---|---|
| fake client's bars and quotes round-trip idempotently | `services/worker/tests/test_options_collector.py::TestTheCollectorOnADatabase::test_a_minute_round_trips_idempotently_in_one_call`, `TestTheIndexBarsOnADatabase::test_intraday_writes_only_closed_minutes_two_calls_idempotent` |
| a quote batch never exceeds 500 symbols or the limiter | `test_kite_options.py::TestOptionQuotes::test_a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token`; `test_options_collector.py::TestThePick::test_a_pick_that_would_need_two_calls_is_refused`; the shared-clock tests above |
| the collector makes no call on a non-trading day | `test_options_collector.py::TestTheCeleryTasksMakeNoCallOnANonTradingDay` (the real task bodies; no adapter is even built) and `TestTheGate` |
| partitions before writing (OP2 note) | `TestTheCollectorOnADatabase::test_both_partitions_exist_before_a_write_in_an_uncreated_month` (Mar/Apr 2028) |
| Tier-1 backfill for a year on the dev stack; full duration in STATUS | ⛁ no Kite. Resumability: `TestTheIndexBarsOnADatabase::test_the_backfill_resumes_from_the_newest_stored_bar`; duration **estimated** (OP3.10): 144 calls, a few minutes, ≈ 2.2 M rows |

### Tests (new)

| File | Tests |
|---|---|
| `packages/providers/tests/test_kite_options.py` | 25 |
| `services/worker/tests/test_options_collector.py` (db-marked half on `baskfy_test`) | 39 |
| **Total** | **64** |

**`tools/ci-local.sh` (one full run, alone, at the end): 16 passed, 1 failed, 3 skipped** (the same
3 web skips as OP1/OP2). The failed step, *Tests, with per-package coverage gates*, failed on exactly
the **three pre-existing non-options tests** OP1 and OP2 recorded — `test_api_admin.py::
TestUserLookupAndOverrides::test_an_override_changes_the_effective_entitlements` (hard-coded
2026-09-21 expiry) and the two `packages/providers` Kite-health tests that fail only in the full run
(`test_cli_doctor.py::TestWithoutCredentials::test_kite_is_reported_unavailable_with_the_reason`,
`test_kite.py::TestHealth::test_unconfigured_is_unavailable_not_an_exception`). Not OP3's; not
weakened. Desk suite, lint + type-check, perf budgets, query plans, reconciliation: PASS. After a last
edit made while CI ran (the minute tasks refuse before opening a DB session when the flag is off,
plus one test), the two OP3 test files, `test_celery_config.py` and `test_no_escape_hatches.py` were
re-run alone: **97 passed**.

### What is NOT done

* **The probe has not run.** OP0 §4's six rows stay "pending" until the orchestrator runs it on the box
  and feeds the JSON back; `chain.oi_unit` stays `UNITS` (OP1.3) until then.
* **The collector is off** (`BASKFY_OPTIONS_COLLECT_ENABLED=false`); no row of `op_chain_snapshot` or
  `op_index_minute` exists anywhere yet. The first real rows follow the orchestrator enabling it.
* **The Tier-1 backfill has not run** (no Kite); the duration is an estimate.
* The worker's existing swing `quotes()` path still takes only the read ceiling, not the quote family
  clock (OP3.5) — not this run's book.
* **The minute tasks share the `default` queue** with the worker's compute/backtest queues
  (`--concurrency=2` in `compose.prod.yml`): a long backtest holding both slots would let minutes
  expire (55 s) unwritten. A dedicated options queue/worker is a compose change — OP14 hardening.
* Not deployed, not pushed.

### What blocks OP4

Nothing in code: OP4's signal cores are pure and read fixtures. The scans' *inputs* on the box
(`op_chain_snapshot`, `op_index_minute`) exist only once the collector is enabled.

## OP4 — The sleeves' signal cores and the scans

**✅, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP4.1–OP4.11. Every test runs from fixtures shaped
as OP3 stores them (`op_index_minute` bars, `op_chain_snapshot` minutes); no live row exists yet.

### What exists (`packages/core/src/baskfy_core/options/`)

| Module | `04` | What it holds |
|---|---|---|
| `bars.py` | preamble | `Bar`, closed minutes only, IST from any zone, inclusive windows, `window_settled` (OP4.3), 5-minute bars aligned to 09:15 named by their last minute (OP4.2), Kaufman's ER (`c_0` = the first bar's open) |
| `structures.py` | §2, §6, §7, §8.2 | `ChainView` (step, ATM, parity forward, Black-76 greeks recomputed per quote — OP4.4), `CandidateLeg` at the attempt-1 limit, `Candidate`, `round_trip` (OP4.5), `SleeveBook` + `size_for`, the `Rejection` codes, `to_json` (strings, never floats) |
| `condor.py` | §3; condor §2, §4, §7 | `observe` (every reason, condor order), `select_short` (delta ∧ range, nearest 0.22, tie further OTM — OP4.7), `build` (wings depth-only, credit floor, sizing, liquidity at the sized quantity, cost test, `RESERVE_EXCEEDED`), `exit_decision` (`HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT`, budget breach, no profit on a stale mark) — one function for both variants |
| `directional.py` | §4 | `ema`/`trend` on **NIFTY 50** (OP4.8), `day_filters`, `find_trigger` (counter-trend breaks recorded, never traded), `build` (one step ITM, delta band, premium cap), `exit_decision` (`HARD_EXIT > STOP > INVALIDATED > TARGET > TIME_STOP`) |
| `expiry_setups.py` | §5 | `range_break` (O3-A; low-ER breaks recorded), `gap_hold` (O3-B), `build` (ATM/100 debit spread, debit cap, slot), invalidation helpers, `exit_decision` |
| `scan.py` | §10 | `scan_all` (five rows, O3-B before O3-A), `wanted_minutes` + `SnapshotBook` (the decision-minute snapshots — OP4.4), states per sleeve (OP4.9, OP4.11), `PAUSED` override, `stale` |

**Each sleeve's signal, as implemented:**

* **O1-M / O1-W** — at `plan_time` (10:00), gap ≤ 0.75 %, 09:15-09:59 range ≤ 0.80 %, the 09:59 close
  inside the 09:15-09:44 range, ER ≤ 0.30, not an event day, 45 bars; then the liquid CE/PE with
  |delta| 0.20-0.25 beyond the opening range nearest 0.22, wings ±150, credit ≥ 25 % of width.
* **O2** — at 09:30, NIFTY 50 prev close vs its EMA20 gives the side; gap ≤ 1 %, 15-minute OR ≤ 0.90 %,
  VIX ≤ 22; trigger = first completed 5-minute close in [09:30, 13:30] beyond OR ± 0.05 % with the
  trend; buy one step ITM on the nearest non-expiring weekly, |delta| 0.50-0.75.
* **O3-A** — 09:15-10:14 range ≤ 1.20 %; first 5-minute close in [10:19, 13:00] beyond it ± 0.05 %
  with ER(09:15→bar) ≥ 0.40; ATM/±100 debit spread on today's expiry, debit ≤ 55 % of width.
* **O3-B** — gap 0.50-1.50 % that never trades through half the gap to 09:44; triggers at 09:45 in
  the gap's direction; same spread. O3-B holds the day if both fire.

**Worker:** `baskfy_worker/options/scan.py` (reads bars, `index_snapshot_daily` `nifty-50`/`india-vix`,
the master, the decision snapshots, the sole tenant's event days/config/sessions/journal count;
upserts `op_scan` on `(user_id, sleeve, ts)`); task **`baskfy.options.scan`** (`acks_late=False`);
Beat `options-scan` every minute 09:00-15:59 mon-fri, `default` queue, `expires=55`, `countdown=20`.
**Gating (OP4.10):** refused before any DB session unless `BASKFY_OPTIONS_SCAN_ENABLED` **and**
`BASKFY_OPTIONS_COLLECT_ENABLED` are true (both default false), inside 09:15-15:30, with
`BASKFY_SOLE_USER_ID` set; then refused on an NSE holiday. **No Kite call, ever** (PACK.11) — so no
limiter share.

### AC → test

| `06` OP4 AC | Test |
|---|---|
| quiet monthly → O1-M `WOULD_TRADE`, to the rupee | `test_options_condor.py::TestTheStructure::test_the_quiet_monthly_condor_to_the_rupee` (credit 39.55, risk/lot ₹8,179.25, round trip ₹198.32 from the eight fills), `test_options_scan.py::TestEachFixtureDay::test_quiet_monthly_o1m_would_trade` |
| weekly trend → O1-W `WOULD_SKIP` (ER, containment), O3-A `TRIGGERED` | `test_options_scan.py::…test_trend_weekly_o1w_skips_with_er_and_containment`, `…test_trend_weekly_o3a_triggers`; `test_options_expiry_setups.py::TestO3ARangeBreak` |
| gap-hold → O3-B | `…test_gap_hold_o3b_triggers_at_0945` (debit 26.95), `TestO3BGapHold` |
| O2 up-trend break; counter-trend seen not traded; Monday → Tuesday's contract; Tuesday → next week's | `…test_o2_up_break_monday_uses_tuesdays_contract` (E ₹106.75), `…test_o2_counter_trend_break_is_seen_not_traded`, `…test_o2_on_a_tuesday_expiry_uses_next_weeks_contract` |
| ER identities; delta ∧ range; never-naked | `test_options_condor.py::TestEfficiencyRatio`, `…test_delta_and_range_neither_relaxed_*`, `TestNeverNaked` + `test_options_expiry_setups.py` (OP1's harness) |
| scan candidate == plan from the same inputs; slot exclusivity | `test_options_scan.py::TestTheCandidateIsThePlan` (O1, O2, O3), `TestTheSlot` |
| scan task idempotent per minute | `services/worker/tests/test_options_scan_task.py::TestTheScanOnADatabase::test_a_minute_writes_one_row_per_sleeve_idempotently` (on `baskfy_test`) |
| (extra) one-way states through whole fixture days | `test_options_scan.py::test_states_are_one_way_through_the_day` |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_bars.py` | 10 |
| `packages/core/tests/test_options_condor.py` | 36 |
| `packages/core/tests/test_options_directional.py` | 34 |
| `packages/core/tests/test_options_expiry_setups.py` | 30 |
| `packages/core/tests/test_options_scan.py` | 37 |
| `services/worker/tests/test_options_scan_task.py` (db-marked half on `baskfy_test`) | 20 |
| **Total** | **167** (+ `options_scan_fixtures.py`, the fixture days) |

Collected after OP4: screener core `packages/core/tests` **6,192**, screener tree **10,491**; desk
unchanged (OP4 touched no desk file). `ruff check`, `ruff format --check`, `mypy` (whole tree, 791
files) clean; `test_no_escape_hatches.py` and `test_options_purity.py` green over the new modules.

**`tools/ci-local.sh` (one full run, alone, at the end): 14 passed, 3 failed, 3 skipped** (the same 3
web skips as OP1-OP3). The three failed steps:

* *Tests, with per-package coverage gates* — both suites ran; exactly the **three pre-existing
  non-options failures** OP1-OP3 recorded (`test_api_admin.py::TestUserLookupAndOverrides::
  test_an_override_changes_the_effective_entitlements`, hard-coded 2026-09-21 expiry; and the two
  `packages/providers` Kite-health tests). Not OP4's; not weakened.
* *Lint and type-check* — one mypy `unreachable` in OP4's own new worker test (a JSON column typed as
  an object holding a list); **fixed**, and `ruff check`, `ruff format --check`, `mypy` (792 files)
  re-run alone afterwards: clean. The worker test file re-run green on `baskfy_test`.
* *Performance budgets* — `services/api/tests/test_load.py::TestFiftyConcurrentScreenRuns` (screener
  API p95 490 ms against 400 ms), and it failed again when re-run alone. OP4 changes no API code and
  nothing the screener reads; OP1 recorded this same step failing as "a latency budget on a loaded
  Mac". Recorded, not OP4's; not weakened.

Desk suite, query plans, reconciliation, Prometheus rules, client and web lint/unit: PASS.

### What is NOT done

* **No live scan has run.** `op_scan` stays empty until the orchestrator enables the collector
  (after the OP3 probe) **and** the scan flag on the box; both default false.
* The daily inputs (`index_snapshot_daily` `nifty-50` and `india-vix`) are assumed present on the box
  from the dashboard snapshot job; if `india-vix` is absent the VIX falls back to the stored minute
  bars, and with neither O2 skips `VIX_UNKNOWN` — not verified on the box.
* **Costs are estimates** (OP4.5); OP6-OP8 pin them to the paisa. **Margin** is not estimated by the
  scan (a broker call; the plan builder's, `04` §7.4).
* The mutation harness does not yet cover the six new modules (OP1.11's `OPTIONS_TARGETS`) — OP14.
* No route reads `op_scan` yet (OP5). No pruning of `op_scan` to 90 days yet (`03` §6) — OP14.
* Not deployed, not pushed.

### What blocks OP5

Nothing in code: OP5's routes read `op_scan` rows whose shape is fixed here (`reasons` text[],
`numbers` and `candidates` JSONB with strings for money). Live rows need the two flags on the box.

## OP5 — API and the web Options tab (the scans ship)

**✅, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP5.1–OP5.10. Nothing here moves money: the tab
reads what the worker (OP3/OP4) wrote and the desk (OP9+) will write, and its two writes are an
event day and the settings.

### What exists

**API** (`services/api/src/baskfy_api/routers/options.py`, reads in `options_read.py`; mounted in
`app.py`; OpenAPI + `packages/api-client` regenerated):

| Route | What it answers |
|---|---|
| `GET /options/today` | today's role per sleeve (`calendar.role` over `op_expiry`, OP5.4), the next six expiries with event days, pauses, NIFTY 50 / India VIX (collector minute else daily close), the newest `op_scan` row per sleeve, open positions, today's closed trades, the week's R per sleeve (real/paper apart), per-group gate (`PAPER` / `DESK_DECIDES`, OP5.5), the clock (`live`, `stale` > 2 min, `as_of_minute`, `scan_date`) and `empty_reason` (OP5.2) |
| `GET /options/scan/{sleeve}` | one sleeve's rows through a session (default the latest scanned) |
| `GET /options/chain?expiry` | nearest two expiries, ±10 strikes around ATM, ΔOI, ATM IV, PCR (OP5.10) |
| `GET /options/positions`, `/sessions?from&to&sleeve`, `/journal?sleeve`, `/backtest?sleeve`, `/calendar?year` | as named; journal never pooled (OP5.7); backtest newest run per sleeve per tier with its caveat verbatim, or a reason |
| `POST` / `DELETE /options/event-day` | mutation 1 of 2 — a person's day (`USER`); a seeded day is refused (OP5.6) |
| `GET` / `PATCH /options/config` | settings, ceilings, gates, every `04` threshold read-only with its anchor, the audit trail; mutation 2 of 2, atomic across parts (OP5.1) |

**Web** (`apps/web`): `/options` (header strip, the four panels — Premium selling O1-M/O1-W,
Directional O2, Expiry-day setups O3-B/O3-A, Positions & paper results — and the collapsed chain),
`/options/journal` (paper-period progress per group, per-pool summaries with R spread, backtest
cards or "Not run yet"), `/options/calendar` (the year's expiries from `op_expiry`, event days, add /
remove your own), `/me/options` (the settings form with the server's ceilings under each box, a 422
rendered as "Most lots: max 10 — set by the server", execution per group "disabled on this server —
paper only", every rule read-only). `lib/options/{types,fetch,write,view}.ts`,
`components/options/*`. `STAFF_BUILD_TABS` is Swing / Volume / Tight / **Options**;
`isSleeveSection("options")`; `SECTION_TABS.options` = Today / Journal / Calendar; `/me/options` in
Me's row; the Operator nav group lists `/options`. No Overlap column (`05` §1).

**The clock** (root `CLAUDE.md`'s clock table gains the options row, this commit): `Live · 13:14`
(amber `· stale` beyond two minutes) while the session is open and the rows are today's; `As of
close, Mon 21 Sep · market closed` otherwise; `Marked 13:14:30` on a position. The shared
`useLiveMarks` overlay touches only the two header index levels (OP5.3).

**With the flags off** (the box now): `/options` renders the header (roles, expiries from
`op_expiry`, index levels from the daily snapshot), five "Not scanned" cards, and the answer "The
options collector is off, so there is no option chain for a scan to read. Nothing below is a
statement about today's market — the strategies have not looked." — never a blank, never an error.
**With them on**: each card shows its state chip, every reason in words, the filters against their
thresholds (green/red), O2's trigger and distance, the counter-trend breaks "seen, not traded", and
each priced candidate (legs, bid/ask, delta, limit, points, lots and sizing mode, max loss, round
trip and cost share).

### AC → test

| `06` OP5 AC | Test |
|---|---|
| read-only test green, both sides | `services/api/tests/test_options_readonly.py` (13: exact verb set, 405 on every other verb against the running app, no execution/broker/auto-execute name, flags reported not branched, app-wide no execute/confirm path, sole tenant on all 12 handlers); `apps/web/src/app/(app)/options/__tests__/read-only.test.tsx` (7: exactly three server actions across the tree, the write union is exactly two paths, no route handler, no execute/confirm/broker word, app-wide no execute/confirm page) |
| `GET /options/today` p95 < 200 ms on the dev stack | `test_api_options.py::TestItIsFastAndItIsOnePersons::test_today_p95_under_200ms` (40 requests, full morning + calendar, in-process ASGI over `baskfy_test`) |
| `/options` from fixtures: O1 `WOULD_SKIP` + reasons, O2 `ARMED` + distance, O3 candidate, `As of close` outside hours | `app/(app)/options/__tests__/page.test.tsx` (rendered DOM over fixtures, OP5.8) + `test_api_options.py::TestTheRowsTheWorkerWrote`, `TestTheClock`; `e2e/options.spec.ts` asserts the empty tab in a browser — **written, not run** (Playwright is skipped by `ci-local.sh`) |
| (extra) empty tab names its reason; calendar from `op_expiry`; the two writes | `TestTheEmptyTabSaysWhy`, `TestTheCalendar`, `TestTheTwoWrites`, `TestTheChain`; web `view.test.ts`, `components/options/__tests__/pages.test.tsx` (no internal name on any options page), `me/options/__tests__/actions.test.ts` |

### Tests (new)

| File | Tests |
|---|---|
| `services/api/tests/test_api_options.py` (db-marked, `baskfy_test`) | 17 |
| `services/api/tests/test_options_readonly.py` | 13 |
| `apps/web/src/lib/options/__tests__/view.test.ts` | 11 |
| `apps/web/src/app/(app)/options/__tests__/page.test.tsx` | 7 |
| `apps/web/src/app/(app)/options/__tests__/read-only.test.tsx` | 7 |
| `apps/web/src/components/options/__tests__/pages.test.tsx` | 4 |
| `apps/web/src/app/(app)/me/options/__tests__/actions.test.ts` | 5 |
| **Total** | **64** (+ `e2e/options.spec.ts`, 2, not run) |

Extended, not rewritten: `test_api_artifacts.py` `EXPECTED_PATHS` (the ten paths), `nav.test.ts`,
`section-tabs-hub.test.tsx`, `section-tabs.test.ts`.

**`tools/ci-local.sh` (one full run, alone, 22:01-22:45 IST, after the deploy window): 14 passed,
3 failed, 3 skipped** (the same 3 web skips as OP1-OP4). The three failed steps:

* *Tests, with per-package coverage gates* — both suites ran; exactly the **pre-existing non-options
  failures** OP1-OP4 recorded: `test_api_admin.py::…test_an_override_changes_the_effective_entitlements`
  (hard-coded 2026-09-21 expiry) and the Kite-health pair (`packages/providers/tests/test_kite.py::
  TestHealth::test_unconfigured_is_unavailable_not_an_exception`,
  `test_cli_doctor.py::TestWithoutCredentials::test_kite_is_reported_unavailable_with_the_reason`).
  No options test failed. Not OP5's; not weakened.
* *Performance budgets* — `test_load.py::TestFiftyConcurrentScreenRuns` (the screener p95 on this
  Mac), the same pre-existing failure. OP5's own budget (`/options/today` p95 < 200 ms) passed in
  the test suite.
* *Fail if the checked-in client is stale* — `git diff --exit-code -- src/generated` compares the
  working tree with the **index**, and the regenerated client was not yet staged; the sibling steps
  "openapi.json is current" and "Generated client is current" passed. Re-run after staging OP5's
  files: **PASS** (`generate:check` exit 0).

Namespace, desk suite, Python lint + mypy, query plans, reconciliation, Prometheus rules, client
type-check/tests, web lint + typecheck and web unit tests: PASS.

### Deploy notes (the scan + collector on the box — for the orchestrator, not done here)

* **No new compose service.** The collector (`options-collect-chain`, `options-index-bars`), the scan
  (`options-scan`), the nightly master (`options-contract-master`) and `options-index-bars-eod` are
  Beat entries (OP2-OP4) on the existing `beat`, run by the existing `worker` on the `default` queue.
* **Order:** run OP3's read-only probe (`python -m baskfy_worker.options_cli probe`) on the box with a
  Kite session; confirm `op_expiry` is filled (the nightly master, or `python -m baskfy_worker.options_cli refresh-master` by hand); then in
  `infra/docker/.env.staging` set `BASKFY_OPTIONS_COLLECT_ENABLED=true` and, once minutes are
  arriving, `BASKFY_OPTIONS_SCAN_ENABLED=true`. `BASKFY_SOLE_USER_ID` must be set (the scan refuses
  without it).
* **Recreate `api`, `worker` and `beat`** after a flip (`docker compose -f compose.prod.yml up -d api
  worker beat`): every service reads the flags once at startup, and the API reads the same two to
  tell the tab *why* it is empty — an API left on the old env would keep saying "collector off" over
  a running collector.
* **Seed the settings** once (`python -m baskfy_worker.options_cli seed`) or `/me/options` says "not
  set up yet". The money flags (`BASKFY_OPTIONS_*_EXECUTION_ENABLED`, `OPTIONS_ENABLED`,
  `INTRADAY_ENABLED`) stay false — Maulik's, never the orchestrator's.
* Check: `/options` should read `Live · HH:MM` within two minutes of 09:16 on a session day.

### What is NOT done

* **No live row has been rendered** — the collector and scan are off on the box; the tab has only
  ever rendered fixtures and an empty `baskfy_test`.
* **The Playwright spec was not run** (OP5.8); the AC's three states are asserted over fixtures.
* The journal, positions and sessions reads have no rows to read until OP9-OP11; their shapes are
  tested empty only (plus the AC morning for positions/closed trades — empty).
* "Consecutive" sessions and "zero rule violations" in the paper progress are not computed (OP11,
  OP15). No per-sleeve Backtest card has data until OP12.
* Not deployed, not pushed.

### What blocks OP6

Nothing in code. OP6's plan builder writes `op_plan`/`op_session`, which `/options/today`'s
`PLANNED` state and `/options/sessions` already read.

## What is NOT done

Everything after OP5. From OP0 itself: the six live Kite reads (OP0 §4, pending a Kite session —
OP3 does them first); the two full-suite baseline runs (OP0.9); the gateway gap of OP0.6 is **fixed in OP2** (OP2.1); the
`/ops` side door of OP0.5 is **recorded, not fixed** (OP13). Every money flag is false and stays false. The paper periods begin only after OP15, and
the longest (O1-M) takes about six months after that.
