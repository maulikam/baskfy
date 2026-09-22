# OP run — live status

The status page for the options run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: OP0 🟡, OP1 ✅, OP2 ✅ (22 Sep 2026); OP3 not started.** Pack written 22 Sep 2026 on branch
`developer`, absorbing the never-started condor pack (`docs/condor/`) as sleeve O1. OP0, OP1 and OP2
were each run alone, by instruction ("execute only OP<N>, then stop"); the next session resumes at OP3.

## Module ledger

| Module | State | One line |
|---|---|---|
| OP0 — Baseline, read-in, verified facts | 🟡 | Costs, expiry circular, F&O segment, algo rules, `OPTIONS_ENABLED` blast radius verified; six live Kite reads pending a session; full suites not re-run (memory rule) |
| OP1 — The shared pure core `baskfy_core.options` | ✅ | 11 modules, 635 tests against `04`; purity, mypy strict, ruff, escape hatches clean; mutation 89.6 % (499/557), every survivor justified |
| OP2 — Schema, NFO master, settings, `options_gates()` | ✅ | `0050_options` (17 tables, `op_sleeve` enum, monthly chain partitions), nightly NFO master + `op_expiry`, verified event-day seed, 9 flags + 6 ceilings in desk/API/worker, `options_gates()`, `OptionsSettings`; gateway product gate tightened (OP0.6); 587 new tests |
| OP3 — Provider reads, index minute bars, collector, limiter | ⬜ | |
| OP4 — Sleeve signal cores and the scans | ⬜ | |
| OP5 — API and the web Options tab (the scans ship) | ⬜ | |
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

## What is NOT done

Everything after OP2. From OP0 itself: the six live Kite reads (OP0 §4, pending a Kite session —
OP3 does them first); the two full-suite baseline runs (OP0.9); the gateway gap of OP0.6 is **fixed in OP2** (OP2.1); the
`/ops` side door of OP0.5 is **recorded, not fixed** (OP13). Every money flag is false and stays false. The paper periods begin only after OP15, and
the longest (O1-M) takes about six months after that.
