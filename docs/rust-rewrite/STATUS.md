# Rust rewrite — status

Live status of the port (README.md for the plan, DECISIONS-RS.md for the calls). Updated by
whoever changes a module's state; loud about what is **not** done.

**States:** `not started` · `goldens exist (n)` — the answer key is in `go/testdata/golden`,
no Rust yet · `declared` — the Rust module file exists (empty) · `ported` — Rust compiles and
its golden tests run · `verified` — every golden for the module green, clippy clean, reviewed.

## Foundation (29 Oct 2026; RS2)

| Item | State |
|---|---|
| `rust/` workspace (edition 2024, resolver 3, toolchain 1.99.1) | done |
| `baskfy-core` crate, module stubs, `CoreError` | done |
| `baskfy-core/tests/law_one.rs` (law 1 in Rust) | green |
| `baskfy-testkit` (load, decode, encode, grade; polars and pandas frames; `List(String)` at RS2) | done |
| Harness proof: all 1,702 goldens parse and decode; every polars and pandas frame round-trips exactly; 47 wide decimals pinned (R1.3, R2.8) | green (RS2) |
| `serde_json` with `float_roundtrip` (golden floats decode to the nearest f64, as Python's `json`) | done (RS2, R2.1) |
| `cargo fmt` / `clippy --all-targets -D warnings` | green (RS2) |
| `cargo test --workspace` | **green** (RS2): 17 test binaries, 455 of 455 graded goldens |
| providers, execution, api (axum), worker, desk, single binary | not started |

## Wave 1 (RS1, RS1.1 — 29 Oct 2026) — swing and options ported, 126 of 126

Graded per function against `go/testdata/golden/L1/{swing,options}` by
`rust/crates/baskfy-core/tests/golden_{swing_a,swing_b,options}.rs`. Each area was also checked
by a differential fuzz against the live Python; the bugs it found are fixed (DECISIONS-RS R1.x).
The stale swing keys were regenerated at RS1.1 (R1.1, resolved).

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `swing.market.exposure_tier` | `swing::market` | 14 | 14 |
| `swing.opening_range.evaluate_trigger` | `swing::opening_range` | 12 | 12 |
| `swing.sizing.size_position` | `swing::sizing` | 11 | 11 |
| `swing.stops.manage` | `swing::stops` | 16 | 16 |
| `swing.setups.detect_setups` (over `with_swing_indicators`) | `swing::setups` | 10 | 10 |
| `swing.plan.build_entries` | `swing::plan` | 16 | 16 |
| `options.condor.exit_decision` | `options::condor` | 8 | 8 |
| `options.costs.charges` | `options::costs` | 4 | 4 |
| `options.execution.never_naked` | `options::execution` | 6 | 6 |
| `options.execution.simulate_fill` | `options::execution` | 6 | 6 |
| `options.gating.options_gates` | `options::gating` | 20 | 20 |
| `options.ledger.journal_figures` | `options::ledger` | 3 | 3 |
| **total** | | **126** | **126** |

## Wave 2 (RS2, 29 Oct 2026) — backtest and ranking ported, 329 of 329

Four lanes, each graded by its own test file and then checked by a differential fuzz against
the live Python (backtest_metrics: 403 random/edge `BacktestResult`s, every `as_dict` and
SHA-256 hash byte-equal; factors: 640 cases at **zero** tolerance, bit-for-bit; rank_frame/
explain: 10,904 cases exact; screener: 30,000 cases, 15 left on one documented class, R2.5).
The fuzz found and the lanes fixed 21 defects that no golden caught (R2.2, R2.4).

**`L1/backtest` — 139 goldens** (the "144" in the wave-2 brief was a miscount: 96 + 43)

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `backtest.month_key` | `backtest::helpers` | 6 | 6 |
| `backtest.month_end` | `backtest::helpers` | 6 | 6 |
| `backtest.year_fraction` | `backtest::helpers` (`WideDecimal`, R1.3) | 5 | 5 |
| `backtest.rebalance_dates` | `backtest::helpers` | 10 | 10 |
| `backtest.shift_schedule` | `backtest::helpers` | 7 | 7 |
| `backtest.raw_weights` | `backtest::helpers` | 8 | 8 |
| `backtest.apply_position_limits` | `backtest::helpers` | 10 | 10 |
| `backtest.CostSpec.charge` | `backtest::helpers` | 5 | 5 |
| `backtest.PricePanel` | `backtest::helpers` | 5 | 5 |
| `backtest.PointInTimeReader` | `backtest::helpers` | 2 | 2 |
| `backtest.AdjustmentCoverage.caveats` | `backtest::helpers` | 7 | 7 |
| `backtest.run_backtest` | `backtest::engine` | 22 | 22 |
| `backtest.run_fragility` | `backtest::engine` | 3 | 3 |
| `backtest_metrics.compute_metrics` | `backtest_metrics` | 9 | 9 |
| `backtest_metrics.metrics_hash` | `backtest_metrics` | 5 | 5 |
| `backtest_metrics.drawdown_series` | `backtest_metrics` | 5 | 5 |
| `backtest_metrics.monthly_returns` | `backtest_metrics` | 5 | 5 |
| `backtest_metrics.downsample_curve` | `backtest_metrics` | 5 | 5 |
| `backtest_metrics.rolling_distribution` | `backtest_metrics` | 7 | 7 |
| `backtest_metrics.round_trips` | `backtest_metrics` | 7 | 7 |
| **total** | | **139** | **139** |

**`L1/ranking` — 190 goldens**

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `factor_registry.FACTORS` / `by_family` / `columns` / `family_counts` | `factor_registry` | 4 | 4 |
| `factor_registry.get` | `factor_registry` | 10 | 10 |
| `factor_registry.sql_for` | `factor_registry` | 10 | 10 |
| `screener.validate_definition` | `screener` | 11 | 11 |
| `screener.canonical_json` | `screener` | 9 | 9 |
| `screener.cache_key` | `screener` | 8 | 8 |
| `screener.resolve_columns` | `screener` | 6 | 6 |
| `screener.build_screen_query` | `screener` | 14 | 14 |
| `screener.build_survivors_query` | `screener` | 5 | 5 |
| `screener.build_ranking_frame_query` | `screener` | 6 | 6 |
| `screener.build_export_query` | `screener` | 5 | 5 |
| `screener.ScreenResult.to_json` | `screener` | 2 | 2 |
| `factors.compute_factors` | `factors` | 12 | 12 |
| `factors.compute_factors_unrounded` | `factors` | 3 | 3 |
| `ranking_engine.family_shares` | `ranking_engine` | 8 | 8 |
| `ranking_engine.effective_weights` | `ranking_engine` | 8 | 8 |
| `ranking_engine.percentile` | `ranking_engine` | 8 | 8 |
| `ranking_engine.target_distance` | `ranking_engine` | 5 | 5 |
| `ranking_engine.target_score` | `ranking_engine` | 5 | 5 |
| `ranking_engine.transform` | `ranking_engine` | 7 | 7 |
| `ranking_engine.add_nse_momentum_score` | `ranking_engine` | 6 | 6 |
| `ranking_engine.rank_frame` | `ranking_engine` | 15 | 15 |
| `ranking_engine.rank_history` | `ranking_engine` | 6 | 6 |
| `ranking_engine.explain` | `ranking_engine` | 13 | 13 |
| `ranking_engine.explain_instrument` | `ranking_engine` | 4 | 4 |
| **total** | | **190** | **190** |

**Python side, unchanged and agreeing (RS2):** `test_swing_goldens.py`, `test_options_goldens.py`,
`test_factors_golden.py`, `test_twt_goldens.py`, `test_vbt_goldens.py` — 295 passed, 15 skipped
(the skips need the 1.7 GB research panel, absent by design). Every dumper (`golden.py swing`,
`golden_options.py`, `golden_backtest.py`, `golden_ranking.py`, `golden_twt.py`,
`golden_vbt.py`, `golden_fno.py`) re-run from the live Python rewrote its keys byte-for-byte:
the committed answer key is what the live code produces today.

**What is still not exact (no golden is red because of it).**

* Screener: Python ints wider than i64 in a screen definition are accepted by pydantic and
  refused by Rust (`int_parsing_size`) — 15 of 30,000 fuzz cases. Needs a decision (R2.5).
* `backtest_metrics`: `np.cov` sums through BLAS in an order that cannot be reproduced; beta/
  alpha can differ by ~1 ulp (far inside 1e-9; no case shows it). Residual (R2.2).
* `backtest::engine`: a benchmark level needing scale > 28 (|level| < ~1e-12 with full float
  digits, or > ~7.9e28) errors in Rust where Python returns a value. Stored levels are
  `numeric(18,4)`, so unreachable from real data (R2.7).
* `ranking_engine`: five edge divergences no golden or production frame exercises (R2.6).
* Two exact-decimal types now exist (`backtest::helpers::WideDecimal`, `screener`'s
  `DecimalValue`); wave 3 should lift one into a shared module first (R1.3, R2.8).

## Wave 3 answer keys (RS2) — ready to port

Dumped from the live Python in the golden_ranking.py pattern (inputs round-tripped through JSON
before the call; a raise stored as `{"raises": {type, message}}`; `stable=True`). Each dumper
was run at least three times, under different `PYTHONHASHSEED`s, with identical shasums.

| Area | Dumper | goldens | functions | size | byte-stable |
|---|---|---:|---:|---:|---|
| `baskfy_core.twt` → `L1/twt/` | `tools/parity/golden_twt.py` | 306 | 49 | 2.8 MB | yes |
| `baskfy_core.vbt` → `L1/vbt/` | `tools/parity/golden_vbt.py` | 326 | 50 | 2.2 MB | yes |
| `baskfy_core.fno` → `L1/fno/` | `tools/parity/golden_fno.py` | 615 | 141 | 9.6 MB | yes (regenerated by the integrator, R2.8) |
| **total** | | **1,247** | **240** | | |

Per module — twt: backtest 41, breadth 21, calendar 28, drift 8, exits 74, indicators 18,
plan 39, published 1, signals 48, sizing 21, sleeve 7. vbt: backtest 42, breadth 19, calendar 38,
config 20, drift 7, exits 41, indicators 15, orders 41, plan 45, published 1, signals 29,
sizing 21, sleeve 7. fno: calendar 51, checklist 22, condor 46, config 34, costs 21, covered 22,
directional 91, directional_retest 24, exits 53, gating 29, journal 20, ledger 29, monitor 39,
research 34, retest 19, scan 30, series 7, sizing 24, underlying 7, vol 13.

**Python behaviour the keys lock in (recorded, not changed — R2.9):** `vbt.backtest.yearly`
raises `IndexError` on an empty result (`yearly.case_003`, `known_bug`); `fno.research.summarise`
raises `TypeError` on one trade (`summarise.case_005`, `retest.run_family.case_003`,
`known_bug`); `twt.breadth.breadth_above_dma(as_of=None)` counts every row, labelled with the
latest session; `twt.exits.tick_floor` rounds a negative price toward zero; vbt
`detect_signals` sorts a null `rank_key` first even descending; `vbt.drift.compare` can return
`-0.0`. 46 twt/vbt decimals (unquantised `r_multiple`, `years`) exceed `rust_decimal`'s scale
and are pinned in `every_golden.rs` (R1.3).

**Conventions a wave-3 porter needs** are in each dumper's docstring and every case's
`meta.notes`: frames sorted by `(instrument_id, date)` where polars does not promise order
(`weekly_closes`, `month_low_back`); polars-expression functions (`bucket_keys`,
`liquidity_floor`, `chartink_scan`, `trend_filters`) are graded as the expression applied to a
stored frame; dataclass properties appear under a `"derived"` key; a `Panel` is rows =
instruments × columns = sessions with `null` for NaN; fno bundles several runs in one case for
`run_family`, `simulate_futures`, `futures_signals`, `debit_trades`, `liquid_base`,
`liquid_symbols`.

## What comes next

1. **Wave 3 — the three sleeves' pure cores**, in parallel lanes, after one shared step: lift
   `WideDecimal` (or the screener's `DecimalValue`) into a shared `baskfy_core` module and
   reuse swing's `StopConfig`/`qulla` (twt and vbt both decode `exits.qulla` as swing's
   `StopConfig`). Then **twt** (306) and **vbt** (326) — each split calendar/indicators/
   signals/breadth → sizing/exits/orders/sleeve/plan/drift → backtest; and **fno** (615), the
   largest, split money paths first (config, gating, costs, sizing, exits, ledger, journal,
   covered, checklist, monitor, calendar, condor) → directional/scan/series/underlying/vol →
   research/retest/directional_retest last (frame-heavy, numpy). Each lane ends with a
   differential fuzz at zero float tolerance, as wave 2's did.
2. **Then the I/O layers, in order:** `providers` (Kite/NSE clients, rate-limited exactly as the
   Python's) → **`execution`** (law 2, the order path: guards → risk → rate limit → journal →
   broker; ported **last of the libraries and most carefully** — `DRY_RUN` default, every
   non-negotiable's test ported before any code, never run against live credentials) →
   `api` (axum) → `worker` → `desk`. Each strangles its Python twin only after its goldens and
   its contract tests pass.

## `baskfy_core` — every Python module

Line counts are `wc -l` of the Python file (comments and docstrings included), measured
29 Oct 2026. Rust modules declared in `baskfy-core` today: `swing::{config, indicators, market,
opening_range, plan, setups, sizing, stops}`, `options::{condor, config, costs, execution,
gating, ledger}`, `backtest::{helpers, engine}`, `backtest_metrics`, `factor_registry`,
`factors`, `ranking_engine`, `screener` (RS2). Python modules with no Rust module of their own
that a port needed (`windows`, `circuits`, `momentum`, `precision`, `instrument_regime`,
`factors_ranking`, `blends`, `ranking`, `screen_definition`, `universes`, `rank_buffer`'s
subset) live as private submodules of the port that needed them (R2.3) and are still `not
started` here.

| Python module | lines | state |
|---|---:|---|
| `baskfy_core` | 3 | not started |
| `baskfy_core.action_recovery` | 229 | not started |
| `baskfy_core.adjustments` | 384 | not started |
| `baskfy_core.allocation_ledger` | 978 | not started |
| `baskfy_core.api_keys` | 178 | not started |
| `baskfy_core.backtest` | 1927 | ported — 96/96 (helpers 71, engine 25) |
| `baskfy_core.backtest_metrics` | 780 | ported — 43/43 |
| `baskfy_core.basket` | 398 | not started |
| `baskfy_core.basket_sizing` | 543 | not started |
| `baskfy_core.blends` | 90 | not started |
| `baskfy_core.breadth` | 93 | not started |
| `baskfy_core.broker_connections` | 324 | not started |
| `baskfy_core.candidate_review` | 592 | not started |
| `baskfy_core.cas_import` | 981 | not started |
| `baskfy_core.cash_ledger` | 472 | not started |
| `baskfy_core.catalyst_tags` | 620 | not started |
| `baskfy_core.circuits` | 128 | not started |
| `baskfy_core.costs` | 144 | not started |
| `baskfy_core.curated_accounting` | 338 | not started |
| `baskfy_core.curated_baskets` | 71 | not started |
| `baskfy_core.curated_dividends` | 150 | not started |
| `baskfy_core.curated_drift` | 329 | not started |
| `baskfy_core.curated_metrics` | 769 | not started |
| `baskfy_core.curated_plans` | 268 | not started |
| `baskfy_core.curated_sip` | 325 | not started |
| `baskfy_core.curated_trending` | 472 | not started |
| `baskfy_core.curated_versions` | 420 | not started |
| `baskfy_core.desk_config` | 109 | not started |
| `baskfy_core.desk_score_service` | 194 | not started |
| `baskfy_core.entitlements` | 291 | not started |
| `baskfy_core.eq_bars` | 109 | not started |
| `baskfy_core.exits` | 1 | not started |
| `baskfy_core.exits.qulla` | 220 | not started |
| `baskfy_core.exposure` | 14 | not started |
| `baskfy_core.exposure.allocation` | 414 | not started |
| `baskfy_core.exposure.regime` | 1184 | not started |
| `baskfy_core.factor_registry` | 1214 | ported — 24/24 |
| `baskfy_core.factors` | 672 | ported — 15/15 |
| `baskfy_core.factors_ranking` | 872 | not started |
| `baskfy_core.fno` | 8 | not started |
| `baskfy_core.fno.calendar` | 148 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.checklist` | 284 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.condor` | 274 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.config` | 486 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.costs` | 131 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.covered` | 125 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.directional` | 639 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.directional_retest` | 495 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.exits` | 167 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.gating` | 106 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.journal` | 279 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.ledger` | 347 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.monitor` | 312 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.research` | 1021 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.retest` | 612 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.scan` | 448 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.series` | 217 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.sizing` | 144 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.underlying` | 123 | goldens exist (L1/fno), not ported |
| `baskfy_core.fno.vol` | 187 | goldens exist (L1/fno), not ported |
| `baskfy_core.grouping_suggestions` | 939 | not started |
| `baskfy_core.gst` | 125 | not started |
| `baskfy_core.instrument_regime` | 166 | not started |
| `baskfy_core.invoice` | 216 | not started |
| `baskfy_core.manager_onboarding` | 117 | not started |
| `baskfy_core.market_hours_cb` | 73 | not started |
| `baskfy_core.models` | 524 | not started |
| `baskfy_core.models.accounts` | 805 | not started |
| `baskfy_core.models.admin` | 146 | not started |
| `baskfy_core.models.auth` | 222 | not started |
| `baskfy_core.models.base` | 73 | not started |
| `baskfy_core.models.billing` | 95 | not started |
| `baskfy_core.models.candidate_review` | 97 | not started |
| `baskfy_core.models.catalyst` | 90 | not started |
| `baskfy_core.models.curated_baskets` | 646 | not started |
| `baskfy_core.models.eq_bars` | 57 | not started |
| `baskfy_core.models.facts` | 210 | not started |
| `baskfy_core.models.fno` | 678 | not started |
| `baskfy_core.models.instrument_watch` | 63 | not started |
| `baskfy_core.models.integrations` | 311 | not started |
| `baskfy_core.models.live` | 194 | not started |
| `baskfy_core.models.market` | 198 | not started |
| `baskfy_core.models.options` | 708 | not started |
| `baskfy_core.models.pipeline` | 65 | not started |
| `baskfy_core.models.ranking` | 67 | not started |
| `baskfy_core.models.reference` | 148 | not started |
| `baskfy_core.models.screens` | 122 | not started |
| `baskfy_core.models.swing` | 866 | not started |
| `baskfy_core.models.twt` | 1100 | not started |
| `baskfy_core.models.vbt` | 712 | not started |
| `baskfy_core.momentum` | 96 | not started |
| `baskfy_core.momentum_scan` | 211 | not started |
| `baskfy_core.nse_momentum` | 242 | not started |
| `baskfy_core.options` | 9 | not started |
| `baskfy_core.options.backtest` | 191 | not started |
| `baskfy_core.options.backtest_suite` | 117 | not started |
| `baskfy_core.options.bars` | 199 | not started |
| `baskfy_core.options.calendar` | 194 | not started |
| `baskfy_core.options.chain` | 225 | not started |
| `baskfy_core.options.condor` | 491 | ported — 8/8 |
| `baskfy_core.options.config` | 441 | ported (exercised through other goldens) |
| `baskfy_core.options.costs` | 191 | ported — 4/4 |
| `baskfy_core.options.directional` | 475 | not started |
| `baskfy_core.options.execution` | 366 | ported — 12/12 (scale-29 limit, R1.3) |
| `baskfy_core.options.executor` | 233 | not started |
| `baskfy_core.options.exits` | 314 | not started |
| `baskfy_core.options.expiry_setups` | 478 | not started |
| `baskfy_core.options.gating` | 115 | ported — 20/20 |
| `baskfy_core.options.greeks` | 172 | not started |
| `baskfy_core.options.journal` | 175 | not started |
| `baskfy_core.options.ledger` | 106 | ported — 3/3 |
| `baskfy_core.options.plan` | 592 | not started |
| `baskfy_core.options.plan_o2` | 539 | not started |
| `baskfy_core.options.plan_o3` | 659 | not started |
| `baskfy_core.options.replay_day` | 445 | not started |
| `baskfy_core.options.risk` | 204 | not started |
| `baskfy_core.options.scan` | 769 | not started |
| `baskfy_core.options.session` | 110 | not started |
| `baskfy_core.options.sizing` | 224 | not started |
| `baskfy_core.options.structures` | 426 | not started |
| `baskfy_core.pdf` | 209 | not started |
| `baskfy_core.portfolio_costs` | 175 | not started |
| `baskfy_core.portfolio_csv` | 379 | not started |
| `baskfy_core.portfolio_graph` | 713 | not started |
| `baskfy_core.portfolio_nav` | 764 | not started |
| `baskfy_core.portfolio_units` | 418 | not started |
| `baskfy_core.precision` | 281 | not started |
| `baskfy_core.pros_cons` | 156 | not started |
| `baskfy_core.public_api` | 292 | not started |
| `baskfy_core.rank_buffer` | 319 | not started |
| `baskfy_core.ranking` | 322 | not started |
| `baskfy_core.ranking_engine` | 889 | ported — 85/85 |
| `baskfy_core.ranking_presets` | 198 | not started |
| `baskfy_core.ranking_research` | 70 | not started |
| `baskfy_core.ranking_selection` | 1102 | not started |
| `baskfy_core.ranking_validation` | 757 | not started |
| `baskfy_core.reconcile` | 778 | not started |
| `baskfy_core.reconciliation` | 861 | not started |
| `baskfy_core.reference_export` | 326 | not started |
| `baskfy_core.risk_free` | 62 | not started |
| `baskfy_core.scan_projection` | 144 | not started |
| `baskfy_core.score` | 224 | not started |
| `baskfy_core.screen_definition` | 714 | not started |
| `baskfy_core.screen_definition_corpus` | 675 | not started |
| `baskfy_core.screen_definition_schema` | 40 | not started |
| `baskfy_core.screen_diff` | 251 | not started |
| `baskfy_core.screener` | 1447 | ported — 66/66 |
| `baskfy_core.sebi_registration` | 175 | not started |
| `baskfy_core.seed_data` | 345 | not started |
| `baskfy_core.sleeves` | 186 | not started |
| `baskfy_core.swing` | 71 | not started |
| `baskfy_core.swing.backtest` | 1308 | not started |
| `baskfy_core.swing.config` | 350 | ported (exercised through other goldens) |
| `baskfy_core.swing.indicators` | 145 | ported (exercised through `detect_setups`) |
| `baskfy_core.swing.journal` | 103 | not started |
| `baskfy_core.swing.market` | 267 | ported — 14/14 |
| `baskfy_core.swing.opening_range` | 198 | ported — 12/12 |
| `baskfy_core.swing.plan` | 499 | ported — 16/16 |
| `baskfy_core.swing.setups` | 425 | ported — 10/10 |
| `baskfy_core.swing.sizing` | 159 | ported — 11/11 |
| `baskfy_core.swing.stops` | 209 | ported — 16/16 |
| `baskfy_core.tenancy` | 57 | not started |
| `baskfy_core.tradebook` | 347 | not started |
| `baskfy_core.trading_calendar` | 185 | not started |
| `baskfy_core.twt` | 282 | not started |
| `baskfy_core.twt.backtest` | 756 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.breadth` | 130 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.calendar` | 95 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.config` | 396 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.drift` | 104 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.exits` | 379 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.indicators` | 184 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.plan` | 550 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.published` | 111 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.signals` | 318 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.sizing` | 184 | goldens exist (L1/twt), not ported |
| `baskfy_core.twt.sleeve` | 148 | goldens exist (L1/twt), not ported |
| `baskfy_core.universes` | 192 | not started |
| `baskfy_core.vbt` | 236 | not started |
| `baskfy_core.vbt.backtest` | 727 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.breadth` | 123 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.calendar` | 128 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.config` | 384 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.drift` | 78 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.exits` | 182 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.indicators` | 201 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.orders` | 196 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.plan` | 445 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.published` | 86 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.signals` | 138 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.sizing` | 161 | goldens exist (L1/vbt), not ported |
| `baskfy_core.vbt.sleeve` | 135 | goldens exist (L1/vbt), not ported |
| `baskfy_core.windows` | 211 | not started |

193 modules, 67550 lines in total. Goldens: 1,702 cases (swing 79, options 47, backtest 139,
ranking 190, twt 306, vbt 326, fno 615). Ported: 21 modules (17 with goldens + 4 configs/
indicators/engine halves), **455 of 455** of their goldens green. Not yet marked `verified`:
that needs Maulik's review of the R1.x/R2.x entries.
