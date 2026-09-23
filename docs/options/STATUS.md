# OP run — live status

The status page for the options run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: OP0 🟡, OP1 ✅, OP2 ✅, OP3 🟡, OP4 ✅, OP5 ✅, OP6 ✅, OP7 ✅, OP8 ✅, OP9 ✅, OP10 ✅ (23 Sep 2026); OP11 not started.** Pack written 22 Sep 2026 on branch
`developer`, absorbing the never-started condor pack (`docs/condor/`) as sleeve O1. OP0-OP7 were each run alone, by instruction ("execute only OP<N>, then stop"); OP8 was run by Maulik's
"continue the OP run from OP8" (23 Sep 2026), which also committed OP7's green work (`00cb48b`), found
uncommitted in the tree. The next session resumes at OP11 (and the orchestrator feeds back OP3's box probe — §"What is NOT done" under OP3).

## Module ledger

| Module | State | One line |
|---|---|---|
| OP0 — Baseline, read-in, verified facts | 🟡 | Costs, expiry circular, F&O segment, algo rules, `OPTIONS_ENABLED` blast radius verified; six live Kite reads pending a session; full suites not re-run (memory rule) |
| OP1 — The shared pure core `baskfy_core.options` | ✅ | 11 modules, 635 tests against `04`; purity, mypy strict, ruff, escape hatches clean; mutation 89.6 % (499/557), every survivor justified |
| OP2 — Schema, NFO master, settings, `options_gates()` | ✅ | `0050_options` (17 tables, `op_sleeve` enum, monthly chain partitions), nightly NFO master + `op_expiry`, verified event-day seed, 9 flags + 6 ceilings in desk/API/worker, `options_gates()`, `OptionsSettings`; gateway product gate tightened (OP0.6); 587 new tests |
| OP3 — Provider reads, index minute bars, collector, limiter | 🟡 | Option quotes with depth/OI, minute bars, basket margins; collector + index-bar tasks; per-family shared limiter proven on Redis. **Probe run on the box 22 Sep 2026 after deploying `1c9a3a5`: five of six reads answered** (evidence `docs/options/evidence/op3-probe-2026-09-22.json`); (b) waits for an expiry to pass. **Collector and scan flags ON on the box since 22 Sep 21:4x** (OP3.11). Backfill not run; live limiter share not measured |
| OP4 — Sleeve signal cores and the scans | ✅ | `bars`, `structures`, `condor`, `directional`, `expiry_setups`, `scan` (pure; each sleeve's day function, `build`, exits); task `baskfy.options.scan` behind the scan **and** collect flags (both false), DB-only, idempotent per minute; 167 new tests; no live data yet (collector off) |
| OP5 — API and the web Options tab (the scans ship) | ✅ | `routers/options.py` (10 paths, 2 money-free mutations, everything else 405), `/options`, `/options/journal`, `/options/calendar`, `/me/options`; Options appended after Tight; honest empty state names the switch; SEBI caveat + "Scan · paper only" on every page; 30 API + 34 web tests; e2e spec written, not run |
| OP6 — O1 plan builder (monthly + weekly), costs pinned | ✅ | `baskfy_core.options.plan` (pure: role → gate → chain → `condor.build` → costs → sizing → margin ceiling → `plan_id`, 10:15 expiry, legs wings-first) + `baskfy_worker.options.plan` (`op_session`/`op_plan`/`op_leg`, two margin-calculator reads, lapse, `OPTIONS_PLAN`); task + Beat `options-plan-o1` **dark** behind the monitor flag; 50 new tests |
| OP7 — O2 plan builder | ✅ | `baskfy_core.options.plan_o2` (pure: role → day filters → trigger → `expiry_for_o2` → `directional.build` over the trigger minute → costs → `plan_id`, exits, `min(+30 min, 13:30)` expiry) + `baskfy_worker.options.plan_o2` (`op_session`/`op_plan`/one `op_leg`, lapse, `OPTIONS_PLAN`); **no broker call at all** (OP7.3); task + Beat `options-plan-o2` **dark** behind the monitor flag; 49 new tests |
| OP8 — O3 plan builder | ✅ | `baskfy_core.options.plan_o3` (pure: role → setup → one-O3-a-day → slot → `expiry_setups.build` over the decision minute → costs → margin ceiling → `plan_id`, exits) + `baskfy_worker.options.plan_o3` (O3-B then O3-A each minute, the margin calculator for the hedged basket, `OPTIONS_PLAN`); task + Beat `options-plan-o3` **dark** behind the monitor flag; 65 new tests (8 on `baskfy_test`) |
| OP9 — Desk process `options_monitor` | ✅ | `baskfy_core.options.exits.evaluate` (pure) + desk `app/strategies/nifty_options.py` (marks from depth ticks, bars from index ticks reconciled to `op_index_minute`, one EXIT plan per position), `app/options_clock.py` (loop with idle evaluation, ≤ 1 quote / 5 s fallback, new legs followed), `app/options_monitor.py` (flag-first runner, `PgPositionStore`); `tools/options/replay.py` + five priced fixtures with an independent answer key; 58 new tests |
| OP10 — Desk page + `/nifty-options/execute` (paper) | ✅ | pure `baskfy_core.options.executor` (entry/exit procedure over a venue, never-naked asserted per attempt) + desk `app/options_execute.py` (gateway-backed venue, depth-ladder paper fills, `PgOptionsStore`, LIVE refused) + `app/options_desk.py` (`/nifty-options`, `/data`, execute, close) + template; the monitor's loop sweeps raised exits; 32 new tests |
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

## OP6 — O1 plan builder (monthly and weekly), costs pinned

**✅, 22 Sep 2026.** Decisions `DECISIONS-OP.md` OP6.1–OP6.7. No order is placed anywhere: a plan is
`ISSUED`, lapses at `expires_at`, and waits for OP10's confirm. Every money flag stays false.

### What exists

* **`packages/core/src/baskfy_core/options/plan.py`** (pure) — `decide_o1(sleeve, market, context,
  snapshot, now)`: role (`NO_SESSION` off an O1 day; `SKIPPED/EVENT_DAY` on an event expiry) →
  clock (`NOT_READY` before 10:00 or while the window/snapshot is not stored; `WINDOW_CLOSED` from
  10:15) → `condor.observe` → `condor.build` over the scan's decision snapshot (`scan.priced_view`,
  renamed from `_view`) → `STALE_CHAIN` → legs in `entry_sequence` order with master symbols and
  depth → `plan_costs` (`04` §6.1 itemised over 8 orders). `finalize_o1(decision, margin, …)`:
  never-naked asserted at every prefix, `sizing.margin_check` (hedged + transient), deterministic
  `plan_id`, `expires_at = min(issued + 30 min, 10:15)`, or `REJECTED_MARGIN`.
* **`config.OPTIONS_COST_RATES_REVIEWED_ON`** = 2026-09-22 (`CostRates.reviewed_on`'s default; OP6.3).
* **`services/worker/src/baskfy_worker/options/plan.py`** — `build_o1_plan` (idempotent per date,
  race-safe insert), `lapse_expired`, `plan_o1_minute`, `plan_gate_free`, `kite_margin_reader`
  (`/margins/basket`, `consider_positions=False`), `plan_alert`.
* **Task `baskfy.options.plan_o1`**, Beat `options-plan-o1` (10:00–10:16 each minute, countdown 40 s,
  expires 55 s), gated on `BASKFY_OPTIONS_MONITOR_ENABLED` **and** `BASKFY_OPTIONS_COLLECT_ENABLED`
  (monitor is false on the box → dark); CLI `options_cli plan [--at ISO]`.
* **`AlertName.OPTIONS_PLAN`** + runbook `decile-blueprint/docs/runbooks/11-options-plan.md`.

### Worked example (the fixture morning — quiet monthly, Tue 27 Oct 2026, 10:00:40)

| seq | leg | symbol | bid / ask | limit |
|---|---|---|---|---|
| 1 | BUY 65 | NIFTY26102724700PE | 5.30 / 5.40 | 5.45 |
| 2 | BUY 65 | NIFTY26102725300CE | 5.65 / 5.80 | 5.85 |
| 3 | SELL 65 | NIFTY26102724850PE | 23.80 / 24.05 | 23.75 |
| 4 | SELL 65 | NIFTY26102725150CE | 27.05 / 27.35 | 27.00 |

Credit 39.65 pts = ₹2,577.25; 1 lot (paper, capital ₹0); max loss ₹7,172.75; risk/lot = R ₹8,172.75;
profit target / stop ₹1,288.63 each; costs ₹198.33 (brokerage 160.00, STT 6.01, NSE 2.87, SEBI 0.01,
IPFT 0.00, stamp 0.12, GST 29.32); cost share 0.1539; margin ₹1,30,903.05 (the probe's shape) with
`MARGIN_POOL_UNSET`; expires 10:15.

### AC → test

| `06` OP6 AC | Test |
|---|---|
| fixture morning → the plan to the rupee | `packages/core/tests/test_options_plan.py::TestTheFixtureMorning` (plan, costs by hand, scan == plan, never naked, plan_id, expiry); `services/worker/tests/test_options_plan_task.py::…test_the_fixture_morning_writes_one_plan_to_the_rupee` (rows on `baskfy_test`) |
| wide short-put spread → `REJECTED_COST` | **scoped** (OP6.1): `TestTheWideShortPut` (→ `REJECTED_NO_SHORT_PUT` / credit only), `TestTheCostTest` (`REJECTED_COST` at C 25.35 with floor 0.10; dormant at the default floor) |
| only in-band call inside the OR → `REJECTED_NO_SHORT_CALL` | `TestTheShortCallInsideTheRange` (a 25,150 spike, gate passes) |
| O1-W on the monthly Tuesday → no session | `TestWhichDaysHaveASession`, `…test_o1w_on_the_monthly_writes_nothing` |
| slot held by O3 → `REJECTED_SLOT_TAKEN` | `TestTheSlotAndThePause`, `…test_a_slot_held_by_o3_skips_with_the_code` |
| idempotent per date | `…test_idempotent_per_date` (same `plan_id`, no second margin read, no second alert) |
| costs pinned; `OPTIONS_COST_RATES_REVIEWED_ON` | `TestTheRatesArePinned` |
| `OPTIONS_PLAN` rendered in Mailpit | `…test_the_alert_renders_through_the_mail_transport` (real `Mailer`, recording transport — no Mailpit container ran) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_plan.py` | 33 |
| `services/worker/tests/test_options_plan_task.py` (6 db-marked on `baskfy_test`) | 17 |
| **Total** | **50** |

**`tools/ci-local.sh` (one full run, alone, 22-23 Sep 2026): 16 passed, 1 failed, 3 skipped** (the
same 3 web skips as OP1-OP5). The one failure is *Tests, with per-package coverage gates* — both
suites ran and the failures are exactly the **three pre-existing non-options** ones OP1-OP5 recorded
(`test_api_admin.py::…test_an_override_changes_the_effective_entitlements`, hard-coded expiry; and
the two `packages/providers` Kite-health tests). No options test failed; nothing was weakened. Desk
tests, Python lint + mypy, query plans, reconciliation, perf budgets, Prometheus rules, client and
web lint/tests: PASS. Targeted before that: `test_options_plan.py` 33, `test_options_plan_task.py`
17 (6 on `baskfy_test`), and the whole `test_options_*` + `test_ops_and_alerts` set green.

### What is NOT done

* **No plan has been built from a live chain.** The Beat entry is dark (monitor flag false on the
  box); nothing was deployed or pushed.
* **Finding:** on expiry day the delta band 0.20–0.25 over 50-point strikes is often empty (the
  fixture sweep across flat/skewed vols 14–39 % left more than half the chains with no in-band
  short beyond the range) — O1 will frequently skip `REJECTED_NO_SHORT_CALL/PUT`. Not changed; a
  Tier-1/3 question for OP12.
* The cost test is dormant for O1 at the default credit floor (OP6.1).
* The desk does not read `op_plan` yet (OP9/OP10); `/options` shows `PLANNED` from the session.
* Mutation harness does not cover `plan.py` (OP14).

### What blocks OP7

Nothing in code. O2's builder can follow the same two-phase shape (`decide` → `finalize`) and reuse
`plan_costs`, `plan_legs`, `lapse_expired` and the alert.

## OP7 — O2 plan builder (the directional sleeve)

**✅, 23 Sep 2026.** Decisions `DECISIONS-OP.md` OP7.1–OP7.8. No order is placed anywhere: a plan is
`ISSUED`, lapses at `expires_at`, and waits for OP10's confirm. Every money flag stays false, and
this sleeve reaches **no broker at all**.

### What exists

* **`packages/core/src/baskfy_core/options/plan_o2.py`** (pure) — `decide_o2(market, context,
  snapshots, now)`: role (`NO_SESSION` off a trading day; `SKIPPED/EVENT_DAY` on an event day) →
  clock (`NOT_READY` before 09:30) → `directional.day_filters` (`04` §4.1, every reason) →
  `directional.find_trigger` (§4.2; `NO_TRIGGER_YET` while armed, `SKIPPED/NO_TRIGGER` once the
  window is final, counter-trend breaks recorded and never traded) → `expiry_for_o2` →
  `directional.build` over the **trigger minute's** snapshot (`scan.priced_view`, OP4.4) →
  `STALE_CHAIN` → one leg with the master's symbol and depth → `plan_costs` (`04` §6.1 itemised
  over 2 orders). `finalize_o2(decision, …)`: never-naked asserted, deterministic `plan_id`,
  `expires_at = min(issued + 30 min, 13:30)`, `04` §4.5's exits (stop, target, invalidation level,
  time stop, hard exit) and §4.6's gap-through worst case. `wanted_minutes` names the one snapshot
  minute to load (the scan's idiom).
* **`plan.py` grew three shared helpers** (OP7.1, no behaviour change): `assert_never_naked`, a
  `Decided` protocol for `plan_id_for`, and the worker's public `leg_row` / `session_values` /
  `write_session_row`.
* **`services/worker/src/baskfy_worker/options/plan_o2.py`** — `build_o2_plan` (idempotent per
  date, race-safe insert), `plan_o2_minute`, `plan_o2_gate_free`, `plan_alert`, `plan_detail`. It
  imports no provider and no order path; `lapse_expired` is O1's, shared.
* **Task `baskfy.options.plan_o2`**, Beat `options-plan-o2` (every minute 09:30–13:34, countdown
  45 s, expires 55 s), gated on `BASKFY_OPTIONS_MONITOR_ENABLED` **and**
  `BASKFY_OPTIONS_COLLECT_ENABLED` (monitor is false on the box → dark); CLI
  `options_cli plan-o2 [--at ISO]`.
* **`AlertName.OPTIONS_PLAN`** now covers O2 (`labels.sleeve`); runbook
  `decile-blueprint/docs/runbooks/11-options-plan.md` gained an O2 section.

### Worked example (the fixture morning — `O2_UP_BREAK`, Mon 19 Oct 2026, 10:05:45)

Up-trend (prev close 25,000 over EMA20 24,810), gap 0.08 %, opening range 25,008–25,032 (0.096 %),
VIX 14. The 10:00–10:04 bar closes 25,050, through 25,032 × 1.0005 = 25,044.52 — the trigger. The
plan prices from the 10:05 chain (spot 25,050, ATM 25,050):

| seq | leg | symbol | bid / ask | limit | delta |
|---|---|---|---|---|---|
| 1 | BUY 65 | NIFTY26102025000CE | 201.65 / 203.70 | 203.75 | 0.5366 |

Tuesday's expiry (20 Oct), one step ITM. Premium 203.75 pts = ₹13,243.75 for 1 lot (paper, capital
₹0); stop 142.63 (risk ₹3,973.13 = R less the ₹300 reserve; risk/lot ₹4,273.13); target 326.00
(₹7,946.25); time stop 45 min at 224.13; invalidation a 5-minute close back below 25,032; hard exit
15:00; **gap-through worst case ₹13,243.75**; costs ₹78.34 over two orders (brokerage 40.00, STT
19.66, NSE 9.36, SEBI 0.03, IPFT 0.00, stamp 0.40, GST 8.89); cost share 0.0099; expires 10:35:45.

### AC → test

| `06` OP7 AC | Test |
|---|---|
| Monday fixture uses Tuesday's contract | `test_options_plan_o2.py::TestTheContract::test_the_monday_before_an_expiry_uses_tuesdays_contract`, and the whole `TestTheFixtureMorning` |
| Tuesday fixture uses next week's | `…::test_an_expiry_tuesday_uses_next_weeks_contract` (20 Oct → the 27 Oct 25,000 CE at 471.95) |
| a counter-trend break plans nothing | `TestTheTrigger::test_a_counter_trend_break_plans_nothing_and_is_recorded` (armed, breaks recorded) + `…test_the_window_closing_with_no_trigger_is_a_skipped_session`; on a database `…test_a_day_that_never_triggers_is_a_skipped_session` |
| `VIX_TOO_HIGH` / `GAP_TOO_BIG` plan nothing with the reason | `TestTheDayFilters` (also `RANGE_TOO_WIDE`, `TREND_FLAT`, `VIX_UNKNOWN`, `EVENT_DAY`, holiday) |
| a 0.45-delta "ITM" strike on a fast day → `REJECTED_DELTA` | `TestTheDelta` (0.4508 on the 25,100 CE; the fixture is OP7.7's spot/forward divergence) |
| the gap-through worst case appears on the plan | `TestTheFixtureMorning::test_the_plan_to_the_rupee` and `…plan_o2_task.py` (`detail.gap_through_inr` = "13243.75") |
| one leg, priced, sized, with its exits | `…test_the_exits_are_04_4_5`, `…test_the_costs_to_the_paisa_from_the_verified_rates`, `…test_the_plan_is_the_scans_candidate` |
| idempotent per date | `…plan_o2_task.py::test_idempotent_per_date` (same `plan_id`, no second alert, one row) |
| dark behind the flags, no broker | `TestTheGate` (both flags, the 09:30–13:34 window, no tenant, the Beat entry, `test_no_order_path_and_no_provider_in_the_builder`, `test_the_task_builds_no_kite_client`) |
| `OPTIONS_PLAN` rendered | `…test_the_alert_renders_through_the_mail_transport` (real `Mailer`, recording transport) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_plan_o2.py` | 33 |
| `services/worker/tests/test_options_plan_o2_task.py` (5 db-marked on `baskfy_test`) | 16 |
| **Total** | **49** |

### What is NOT done

* **No O2 plan has been built from a live chain.** The Beat entry is dark (monitor flag false on
  the box); nothing was deployed or pushed.
* **Finding (OP7.6):** at `04`'s defaults the **premium cap and the cost test cannot bind** for
  O2, and at the fixture's ₹4,273 risk per lot `REJECTED_BUDGET` binds until sleeve capital reaches
  ≈ ₹8.5 lakh (≈ ₹17 lakh while the first-live multiplier halves the budget). A number for the
  human track, not an agent's.
* **Finding (OP7.5):** a trigger whose bar closes at or after 13:30 is never planned, and one after
  ≈ 13:25 leaves under five minutes to confirm. Widening it is a `04` §4.2 or non-negotiable-1
  change.
* **Finding (OP7.7):** a low-side `REJECTED_DELTA` can only happen when the index print and the
  chain's parity forward disagree — worth watching once real minutes exist (OP12).
* The desk does not read O2's `op_plan` yet (OP9/OP10); `/options` shows the scan, not the plan.
* Exits are *written on the plan*; nothing evaluates them yet (OP9 runs the monitor).
* Mutation harness does not cover `plan_o2.py` (OP14).

### What blocks OP8

Nothing in code. O3's builder can follow the same three modules (`plan.py`'s shared vocabulary,
`plan_o2.py`'s shape) and reuse `plan_costs`, `plan_legs`, `assert_never_naked`, `session_values`,
`write_session_row`, `lapse_expired` and the alert; it adds the day's slot (`04` §8.6), the
two-leg debit spread's sequences (§5.4) and `REJECTED_DEBIT`.

## OP8 — O3 plan builder (the expiry-day setups)

**✅, 23 Sep 2026.** Decisions `DECISIONS-OP.md` OP8.1–OP8.5. No order is placed anywhere: a plan is
`ISSUED`, lapses at `expires_at`, and waits for OP10's confirm. Every money flag stays false. The one
broker read is the margin **calculator** for the hedged two-leg basket.

### What exists

* **`packages/core/src/baskfy_core/options/plan_o3.py`** (pure) — `decide_o3(sleeve, market,
  context, snapshots, now)`: role (`NO_SESSION` off an expiry; `SKIPPED/EVENT_DAY` on an event
  expiry; `NO_SESSION/SETUP_DISABLED`) → clock (`NOT_READY/BEFORE_PLAN_TIME` before 09:45 for O3-B,
  10:19 for O3-A) → the setup by the scan's own functions (`expiry_setups.gap_hold` /
  `range_break`; `WINDOW_NOT_SETTLED`, `NO_TRIGGER_YET`, `SKIPPED/NO_TRIGGER` and every setup
  reason) → `WINDOW_CLOSED` past the entry window → the decision minute's chain (09:45 for O3-B; the
  minute after the trigger bar for O3-A) → one-O3-a-day (`O3B_HOLDS`, OP8.2) and the expiry-day
  slot → `expiry_setups.build` (ATM long, ±100 short, `REJECTED_DEBIT` above 0.55 × width, sizing,
  liquidity, cost) → `STALE_CHAIN` → two legs, long first → `plan_costs` (4 orders).
  `finalize_o3(decision, margin, …)`: never-naked asserted, `margin_check` on the hedged figure
  (`MARGIN_UNKNOWN` on paper, `REJECTED_MARGIN` live), deterministic `plan_id`, `expires_at =
  min(issued + 30 min, 10:00 | 13:30)`, and `04` §5.3's exits (target value, stop value,
  invalidation rule and level to the paisa, hard exit 14:45).
* **`services/worker/src/baskfy_worker/options/plan_o3.py`** — `build_o3_plan` (idempotent per date
  and setup, race-safe insert, the calculator asked only for a `PLANNED` decision),
  `plan_o3_minute` (lapse, then O3-B, then O3-A), `plan_o3_gate_free` (09:45–13:34), `plan_alert`,
  `plan_detail`. It imports no provider and no order path; the task hands it
  `kite_margin_reader` only when a Kite session is usable.
* **Task `baskfy.options.plan_o3`**, Beat `options-plan-o3` (every minute 09–13h, countdown 45 s,
  expires 55 s), gated on `BASKFY_OPTIONS_MONITOR_ENABLED` **and** `BASKFY_OPTIONS_COLLECT_ENABLED`
  (monitor is false on the box → dark); CLI `options_cli plan-o3 [--at ISO]`.
* **`config.expiry_setups.o3a_entry_window_end = 13:30`** (OP8.1), in `04` §5.1 and §14.
* **`plan.ask_margin`** takes any decision with `margin_baskets()` (OP8.4); O1 unchanged.
* Runbook `docs/runbooks/11-options-plan.md` gained an O3 section.

### Worked example (the gap-hold expiry — `GAP_HOLD`, Tue 13 Oct 2026, 09:46:20)

Previous close 25,000; open 25,200 (+0.80 %), half-gap 25,100, held to the 09:44 bar. The plan
prices from the 09:45 chain (spot 25,190, ATM 25,200):

| seq | leg | symbol | bid / ask | limit |
|---|---|---|---|---|
| 1 | BUY 65 | NIFTY26101325200CE | 31.60 / 31.95 | 32.00 |
| 2 | SELL 65 | NIFTY26101325300CE | 5.00 / 5.10 | 4.95 |

Debit 31.95 − 5.00 = 26.95 of a 100-point width (cap 55.00) → max loss ₹1,751.75 for 1 lot (paper,
capital ₹0); risk/lot ₹2,251.75 with the ₹500 reserve; target value 80.00 (₹3,448.25); stop value
13.48 (₹875.88); invalidation at 25,100.00; hard exit 14:45; costs ₹100.04 over four orders (cost
share 0.0290); expires 10:00. O3-A on `TREND_WEEKLY` (Tue 20 Oct): the 10:15–10:19 bar breaks
25,182 × 1.0005; the 10:20 chain gives the 25,200/25,300 CE spread at 33.55, expiring 10:51:20.

### AC → test

| `06` OP8 AC | Test |
|---|---|
| fixtures for each setup and each direction plan the expected strikes and debit | `test_options_plan_o3.py::TestEachSetupAndDirection` (O3-B up/down, O3-A up/down by the rule; O3-B up and O3-A up to the rupee; the plan equals the scan's candidate) |
| a debit above 55 % of width → `REJECTED_DEBIT` | `TestTheDebitCap` (a chain whose forward sits 80 points above spot: debit > 55; and the cap read from config) |
| O3-A and O3-B both firing → O3-B holds | `TestOneO3ADay` (a fixture that fires both; `PLANNED`/`CONFIRMED`/`OPEN`/`CLOSED` O3-B → O3-A `REJECTED_SLOT_TAKEN, O3B_HOLDS`; `LAPSED`/`SKIPPED` frees it) + on a database `test_options_plan_o3_task.py::…test_one_o3_a_day_on_the_database` and `…test_an_unconfirmed_o3b_lapses_and_frees_o3a` |
| a confirmed O1 → O3 `SLOT_TAKEN` | `TestTheExpiryDaySlot` (O1-W and O1-M holders × both setups; a merely planned O1 does not hold it) |
| two legs, long first, with exits and lots | `TestThePlan`, and on a database `…test_the_gap_hold_expiry_writes_one_spread_to_the_rupee` (session, plan, both legs in send order, the calculator asked once about exactly those two legs) |
| margin (`04` §7.4) | `TestMargin`, `…test_a_failing_calculator_is_a_warning_on_paper` |
| idempotent per date; lapses; alert | `…test_idempotent_per_date`, `…test_the_minute_lapses_an_expired_plan`, `…test_the_alert_renders_through_the_mail_transport` |
| dark behind the flags, no order path | `TestTheGate` (both flags, the window, no tenant, no database session when refused, the Beat entry, the order-path scan, O3-B decided before O3-A) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_plan_o3.py` | 46 |
| `services/worker/tests/test_options_plan_o3_task.py` (8 db-marked on `baskfy_test`) | 19 |
| **Total** | **65** |

**Regression, with Postgres and Redis up locally (`infra/docker/compose.yml`, 23 Sep 2026):** every
`options` test in core (1,085), worker (`options` + `ops_and_alerts`, 259) and API (72): **0 failed,
0 skipped**; the escape-hatch scan and `test_options_docs_parity.py` green; ruff, format and mypy
clean on every touched file. The providers suite (368) is green too, including the six Kite-lane
tests that need Redis. **Not run:** `tools/ci-local.sh` and the desk suite (OP8 touched neither the
desk nor anything it imports).

### What is NOT done

* **No O3 plan has been built from a live chain.** The Beat entry is dark (monitor flag false on
  the box); nothing was deployed or pushed.
* The plan's stop value is written from the *planned* debit; OP9's exit engine must re-derive it
  from the filled debit (`04` §5.3 — "`D` = the entry debit from fills").
* The desk does not read O3's `op_plan` yet (OP9/OP10); `/options` shows the scan, not the plan.
* Mutation harness does not cover `plan_o3.py` (OP14).

### What blocks OP9

Nothing in code. Every sleeve now writes `op_plan`/`op_leg` with its exits on the plan; OP9's
`options_monitor` reads them, subscribes the legs, and evaluates each sleeve's exits
(`condor`, `directional`, `expiry_setups.exit_decision`) under the confirm.

## OP9 — The desk process `options_monitor` (positions and exits)

**✅, 23 Sep 2026.** Decisions `DECISIONS-OP.md` OP9.1–OP9.6. The process holds no gateway; it raises
EXIT plans, and OP10's executor will send them. Every flag stays false.

### What exists

* **`packages/core/src/baskfy_core/options/exits.py`** (pure) — `evaluate(position, marks, index,
  now)`: feed loss first (`HARD_EXIT/FEED_LOST`, §8.5), then the conservative mark, staleness, the
  marked loss and each structure's own rule (`condor.exit_decision`, `directional.exit_decision`,
  `expiry_setups.exit_decision`) with the invalidation bars they read; `mark`, `marked_loss_inr`,
  `latest_five_minute_close`, `latest_bar_after` (OP9.4).
* **`kite-momentum-rebalancer/app/strategies/nifty_options.py`** — `NiftyOptionsMonitor`
  (`generate_targets` → `[]`, no gateway): minute bars from NIFTY 50 ticks, reconciled every 60 s
  to `op_index_minute`; leg marks from depth ticks; `check(now)` on every tick and idle pass; one
  exit plan per position; marks written every 30 s; `on_start` resumes from the store.
* **`app/options_clock.py`** — `run_session` (drain, B10 fallback when quiet 5 s, `check` every
  pass, new tokens followed, `now`/`sleep` seams) and `LegQuotes` (≤ 1 `quote_raw` / 5 s, by token).
* **`app/options_monitor.py`** — `build_monitor` (flag off → not constructed), `PgPositionStore`
  (`open_positions`, `raise_exit`, `record_mark`, `index_minutes` over `public.op_*`),
  `position_from_rows`, `main()` (flag before any import of the database, the ticker or Kite).
* **`tools/options/replay.py`** + **`make_fixtures.py`** + five fixtures under
  `tools/options/fixtures/` with their expected exits (OP9.5).

### AC → test (`kite-momentum-rebalancer/tests/test_options_monitor.py` unless named)

| `06` OP9 AC | Test |
|---|---|
| flag off → not instantiated | `TestTheFlag` (a spy on `__init__`; default false; `main` returns 0 and checks the flag before importing the world) |
| O1 fixture → `PROFIT` at the expected minute | `TestTheReplays` — `o1-profit` → PROFIT at 11:14:50 |
| O2 fixture → `TIME_STOP` | `o2-time-stop` → TIME_STOP at 10:51:00 (45 minutes after the 10:06 entry) |
| O3-A fixture → `TARGET` | `o3a-target` → TARGET at 10:40:50 |
| index feed dies at 14:05 with O1 open → `HARD_EXIT / FEED_LOST` | `o1-feed-lost` → 14:05:50; and `TestTheLoop::test_an_idle_pass_judges_a_silent_feed` (no ticks at all: the loop's clock raises it) |
| restart at 11:30 resumes from `op_position` | `TestTheRestart` (the position from the store, the morning's bars back from `op_index_minute`, the rules' exit from 11:30) |
| a source scan finds no `place(` | `TestNothingPlaces` (strategy, runner and clock; whole-word verbs; `quote_raw`, never `kc.quote(`) |
| (the store's SQL) | `TestTheStoreOnADatabase` — on `baskfy_test`: open positions with legs and the plan's range, one EXIT plan however often raised, the mark written |
| (the composition) | `packages/core/tests/test_options_exits.py` (24) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_exits.py` | 24 |
| `kite-momentum-rebalancer/tests/test_options_monitor.py` (1 on `baskfy_test`) | 34 |
| **Total** | **58** |

**Regression (Postgres and Redis up, 23 Sep 2026):** the **whole desk suite, 2,258 passed**, 17
skipped as before (the swing suite included — see OP9.5 for the name collision found and fixed);
core `options`/escape-hatch/purity tests 1,171 passed; ruff (pyflakes) clean on every new desk and
tools file; ruff + mypy strict clean on `exits.py` and its test.

### What is NOT done

* **No position exists to monitor**: OP10 writes `op_position` when a paper plan fills. Until then the
  monitor runs with nothing to watch, and its exit plans have no executor.
* The compose service and scheduling loop are OP15's (OP9.6); the process runs by hand.
* Plans are still raised by the worker, not the desk (OP9.1).
* The replay fixtures are modelled quotes (Black-76, flat vol); no recorded live session exists yet.
* Mutation harness does not cover `exits.py` (OP14).

### What blocks OP10

Nothing in code. OP10's executor reads `op_plan` (`kind='ENTRY'` to confirm, `kind='EXIT'` to
close), sends legs through the gateway's dry-run branch in `entry_sequence` / `exit_sequence`, writes
`op_order`/`op_fill`/`op_position`, and closes the position the monitor raised an exit for.

## OP10 — The desk page and `/nifty-options/execute` (paper)

**✅, 23 Sep 2026.** Decisions `DECISIONS-OP.md` OP10.1–OP10.8. Every leg is an
`OrderGateway.place(exchange="NFO", product="MIS", order_type="LIMIT")` on a gateway gated by the
sleeve's own switches; with every flag false each comes back `DRY_RUN` and the broker is never
called (a spy proves it under both `DRY_RUN` values). LIVE is refused (OP10.3).

### What exists

* **`packages/core/src/baskfy_core/options/executor.py`** (pure) — `run_entry` / `run_exit` over a
  `Venue`: longs first, attempt 1 + reprice + cancel, abandon-and-close on any short leg, shorts
  first on exit, the marketable third attempt for a short buy-back or O2's sell, `PARTIAL_EXIT` for a
  wing that will not fill; `never_naked` asserted before every send.
* **`kite-momentum-rebalancer/app/options_execute.py`** — `execute_entry` (400/404/409/410 refusals,
  LIVE refused, the expiry-day slot taken on confirm and freed on abandon), `execute_exit`,
  `run_pending_exits`, `DeskVenue`, `PgOptionsStore` (`op_order` per attempt, `op_fill` per fill,
  `op_leg` totals, `op_position` on OPEN, session and plan states), `options_gateway` (shared risk,
  OP10.2), `kite_quotes`.
* **`app/options_desk.py`** + **`app/templates/nifty_options.html`** — `GET /nifty-options`,
  `GET /nifty-options/data`, `POST /nifty-options/execute`, `POST /nifty-options/close`; mounted in
  `app/main.py`.
* **`app/options_clock.run_session(act=…)`** and the runner's exit sweep (OP10.7).

### AC → test

| `06` OP10 AC | Test |
|---|---|
| O1 confirm → four simulated fills wings-first and `OPEN` | `kite-momentum-rebalancer/tests/test_options_execute.py::TestTheConfirm::test_o1_confirm_is_four_simulated_fills_wings_first_and_open` (orders, fills, position, slot, credit 29.80 from the ladder) |
| thin long-call depth → `ABANDONED_ENTRY`, no short ever sent | `TestAbandonment::test_thin_long_call_depth_abandons_…` (+ core `test_options_executor.py::TestAbandonment`) |
| O3 partial long → abandoned, no short sent | `TestAbandonment::test_an_o3_partial_long_is_abandoned_…` (net open 0 on both legs) |
| an exit produces closes in sequence with the final attempt marketable | `TestTheExit` (shorts first, position and session closed, the exit plan `CONFIRMED`, a second sweep empty) + core `TestTheExit` (the marketable third attempt; a wing gets none) |
| gateway spy: 0 broker calls under both `DRY_RUN` values with the execution flag false | `TestNoBrokerCall` (parametrised over `DRY_RUN`) and every other desk test's spy |
| never-naked across 500 seeded fill sequences per structure | `packages/core/tests/test_options_executor.py::TestNeverNaked` (condor, spread, long) |
| no path calls `place_gtt_stop` | `TestNoGtt` (+ NFO/MIS/LIMIT through `gateway.place`, two POSTs, the sentences verbatim) |
| the page | `TestThePage` (the plan, "Confirm — simulated", the O3 sentence; `confirm` not `true` → 400 before the store is opened) |

### Tests (new)

| File | Tests |
|---|---|
| `packages/core/tests/test_options_executor.py` | 10 (incl. 3 × 500 seeds) |
| `kite-momentum-rebalancer/tests/test_options_execute.py` (on `baskfy_test`) | 17 |
| **Total** | **27** |

**Regression (Postgres + Redis up, 23 Sep 2026):** the **whole desk suite: 2,275 passed**, 17 skipped
as before; ruff + mypy strict clean on `executor.py` and its test; pyflakes clean on the new desk files.

### What is NOT done

* **LIVE execution is not built** (OP10.3): the live venue must read fills back from the broker.
* `05` §3's status bar (the four flags, Kite token, hard-exit countdown), the mark on a stop–target
  bar, spot against the levels, and the ledger panel (OP11) are not on the page (OP10.8).
* Paper fills do not wait `fill_wait_seconds` (OP10.5); the journal row (`op_journal`) is OP11's.
* The monitor process and its exit sweep still run by hand (OP9.6); nothing is deployed.

### What blocks OP11

Nothing in code. Closed sessions now carry their fills; OP11 writes `op_journal` in ₹ and R from them
and enforces the pauses and the first-live multiplier.

## What is NOT done

Everything after OP10. From OP0 itself: the six live Kite reads (OP0 §4, pending a Kite session —
OP3 does them first); the two full-suite baseline runs (OP0.9); the gateway gap of OP0.6 is **fixed in OP2** (OP2.1); the
`/ops` side door of OP0.5 is **recorded, not fixed** (OP13). Every money flag is false and stays false. The paper periods begin only after OP15, and
the longest (O1-M) takes about six months after that.
