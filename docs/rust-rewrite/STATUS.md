# Rust rewrite — status

Live status of the port (README.md for the plan, DECISIONS-RS.md for the calls). Updated by
whoever changes a module's state; loud about what is **not** done.

**States:** `not started` · `goldens exist (n)` — the answer key is in `go/testdata/golden`,
no Rust yet · `declared` — the Rust module file exists (empty) · `ported` — Rust compiles and
its golden tests run · `verified` — every golden for the module green, clippy clean, reviewed.

## Foundation (29 Oct 2026; RS2, RS3)

| Item | State |
|---|---|
| `rust/` workspace (edition 2024, resolver 3, toolchain 1.99.1) | done |
| `baskfy-core` crate, module stubs, `CoreError` | done |
| `baskfy-core/tests/law_one.rs` (law 1 in Rust) | green |
| `baskfy-testkit` (load, decode, encode, grade; polars and pandas frames; `List(String)` at RS2) | done |
| Harness proof: all 1,702 goldens parse and decode; every polars and pandas frame round-trips exactly; 47 wide decimals pinned (R1.3, R2.8) | green (RS3) |
| `serde_json` with `float_roundtrip` (golden floats decode to the nearest f64, as Python's `json`) | done (RS2, R2.1) |
| `cargo fmt` / `clippy --all-targets -D warnings` | green (RS4a) |
| `cargo test --workspace` | **green** (RS4a): 45 test targets, 668 passed + 14 ignored (6 opt-in fuzz harnesses, 8 Parquet-fixture cases, R4.12); core 222, providers 437, testkit 9; **1,702 of 1,702 graded goldens** |
| Pure core, golden-covered part (swing, options, backtest, ranking, twt, vbt, fno) | **complete** (RS3) — every committed golden green; the 128 `baskfy_core` modules with no goldens are still `not started` (table below) |
| `baskfy-providers` (foundation RS4.0; kite, nse, archive, composite, fixtures, factory RS4a) | **ported** (RS4a): 437 tests green, 8 ignored until Parquet lands (R4.12); residuals below |
| `baskfy-db` (read-only sqlx pool, typed rows; RS4b foundation) | **done** (RS4b): 10 unit + 8 bed tests green; every connection refuses writes (`25006`, proven on the bed) |
| `baskfy-api` foundation (axum; settings, pipeline, problems, auth, CSRF check, rate limit, ETag, CORS, 44 route modules) | **done** (RS4b): 67 tests green (5 of them need the bed); `api_diff.py` **55 of 55 EQUAL** on `foundation.json` |
| `cargo test --workspace` at RS4b | **green**: 58 test targets, 753 passed + 14 ignored (the same 14 as RS4a; built with `BASKFY_RS_TEST_DATABASE_URL` set, so no bed test is ignored); **1,702 of 1,702 graded goldens** |
| API routes (the five lanes), worker, desk, execution, single binary | not started (next: the API lanes over `routes/*.rs`, graded by `tools/parity/api_diff.py`) |

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
  refused by Rust (`int_parsing_size`) — 15 of 30,000 fuzz cases. Decided at RS3 as a
  documented divergence (R2.5, ⚠ UNREVIEWED).
* `backtest_metrics`: `np.cov` sums through BLAS in an order that cannot be reproduced; beta/
  alpha can differ by ~1 ulp (far inside 1e-9; no case shows it). Residual (R2.2).
* `backtest::engine`: a benchmark level needing scale > 28 (|level| < ~1e-12 with full float
  digits, or > ~7.9e28) errors in Rust where Python returns a value. Stored levels are
  `numeric(18,4)`, so unreachable from real data (R2.7).
* `ranking_engine`: five edge divergences no golden or production frame exercises (R2.6).
* Two exact-decimal types now exist (`backtest::helpers::WideDecimal`, `screener`'s
  `DecimalValue`); wave 3 should lift one into a shared module first (R1.3, R2.8).

## Wave 3 (RS3, 29 Oct 2026) — the three sleeves' pure cores ported, 1,247 of 1,247

Four lanes (twt, vbt, fno core+trade, fno research), each graded by its own test files —
`rust/crates/baskfy-core/tests/golden_{twt_a,twt_b,vbt_a,vbt_b,fno_a,fno_b,fno_c}.rs` — and
each then checked by a differential fuzz against the live Python at zero float tolerance (the
five `#[ignore]`d tests in those files are the fuzz harnesses, which need a generated directory;
they are not golden cases). The integrator checked that every one of the 240 golden function
names is graded by a test file; fno-a and fno-b (`table_covers_every_golden_function`) and twt-b
(`every_lane_b_golden_is_graded`) also assert it in code. The fuzz found and the lanes fixed defects no golden caught (R3.2, R3.6, R3.7).

**Integrator's run (RS3):** `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`
and `cargo test --workspace` green — 24 test targets, 221 tests passed, 5 ignored (the fuzz
harnesses), **1,702 of 1,702 graded goldens** (455 from waves 1–2, 1,247 new). Python's golden
suites (`test_{swing,options,twt,vbt}_goldens.py`, `test_factors_golden.py`): 295 passed, 15
data-gated skips. `golden_{twt,vbt,fno}.py` re-run from the live Python into a scratch
directory: all 1,247 keys byte-identical with the committed ones.

| Lane | Rust | goldens | passing |
|---|---|---:|---:|
| twt (12 modules) | `twt::*` | 306 | 306 |
| vbt (13 modules) | `vbt::*` | 326 | 326 |
| fno core + trade (17 modules) | `fno::*` | 538 | 538 |
| fno research (`research`, `retest`, `directional_retest`) | `fno::*` | 77 | 77 |
| **total** | | **1,247** | **1,247** |

The fno-b porter reported 309 goldens; the directory holds 303 (condor 46, exits 53, covered 22,
checklist 22, monitor 39, directional 91, scan 30) and the runner table asserts exactly that —
a reporting slip, no code change.

**`L1/twt` — 306 goldens**

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `twt.backtest.gate_vector` | `twt::backtest` | 4 | 4 |
| `twt.backtest.panel_from_frame` | `twt::backtest` | 5 | 5 |
| `twt.backtest.run_backtest` | `twt::backtest` | 15 | 15 |
| `twt.backtest.summarise` | `twt::backtest` | 11 | 11 |
| `twt.backtest.yearly` | `twt::backtest` | 6 | 6 |
| `twt.breadth.as_shared_config` | `twt::breadth` | 2 | 2 |
| `twt.breadth.breadth_above_dma` | `twt::breadth` | 7 | 7 |
| `twt.breadth.breadth_series` | `twt::breadth` | 5 | 5 |
| `twt.breadth.gate_for` | `twt::breadth` | 7 | 7 |
| `twt.calendar.as_shared_config` | `twt::calendar` | 2 | 2 |
| `twt.calendar.build_calendar` | `twt::calendar` | 6 | 6 |
| `twt.calendar.drop_thin_sessions` | `twt::calendar` | 5 | 5 |
| `twt.calendar.session_counts` | `twt::calendar` | 5 | 5 |
| `twt.calendar.thin_sessions` | `twt::calendar` | 10 | 10 |
| `twt.drift.compare` | `twt::drift` | 8 | 8 |
| `twt.exits.clamp_under_close` | `twt::exits` | 7 | 7 |
| `twt.exits.fill_day_stop` | `twt::exits` | 6 | 6 |
| `twt.exits.initial_stop` | `twt::exits` | 6 | 6 |
| `twt.exits.manage` | `twt::exits` | 9 | 9 |
| `twt.exits.on_adjustment` | `twt::exits` | 5 | 5 |
| `twt.exits.r_multiple` | `twt::exits` | 7 | 7 |
| `twt.exits.ratchet` | `twt::exits` | 8 | 8 |
| `twt.exits.stop_fill` | `twt::exits` | 7 | 7 |
| `twt.exits.stop_is_below_entry` | `twt::exits` | 6 | 6 |
| `twt.exits.tick` | `twt::exits` | 3 | 3 |
| `twt.exits.tick_floor` | `twt::exits` | 7 | 7 |
| `twt.exits.trail_from` | `twt::exits` | 3 | 3 |
| `twt.indicators.bucket_keys` | `twt::indicators` | 4 | 4 |
| `twt.indicators.require_columns` | `twt::indicators` | 5 | 5 |
| `twt.indicators.with_twt_indicators` | `twt::indicators` | 9 | 9 |
| `twt.plan.assemble` | `twt::plan` | 4 | 4 |
| `twt.plan.build_entries` | `twt::plan` | 19 | 19 |
| `twt.plan.client_id_for` | `twt::plan` | 4 | 4 |
| `twt.plan.exit_lines` | `twt::plan` | 5 | 5 |
| `twt.plan.gtt_limit_price` | `twt::plan` | 4 | 4 |
| `twt.plan.stop_for` | `twt::plan` | 3 | 3 |
| `twt.published.PUBLISHED` | `twt::published` | 1 | 1 |
| `twt.signals.detect_signals` | `twt::signals` | 5 | 5 |
| `twt.signals.entry_events` | `twt::signals` | 7 | 7 |
| `twt.signals.liquidity_floor` | `twt::signals` | 3 | 3 |
| `twt.signals.month_low_back` | `twt::signals` | 5 | 5 |
| `twt.signals.signal_mask` | `twt::signals` | 3 | 3 |
| `twt.signals.tight_state` | `twt::signals` | 5 | 5 |
| `twt.signals.weekly_closes` | `twt::signals` | 8 | 8 |
| `twt.signals.weekly_column` | `twt::signals` | 4 | 4 |
| `twt.signals.with_twt_columns` | `twt::signals` | 8 | 8 |
| `twt.sizing.first_live_multiplier` | `twt::sizing` | 6 | 6 |
| `twt.sizing.size_entry` | `twt::sizing` | 15 | 15 |
| `twt.sleeve.sleeve_value` | `twt::sleeve` | 7 | 7 |
| **total** | | **306** | **306** |

**`L1/vbt` — 326 goldens**

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `vbt.backtest.BacktestResult.fill_rate_pct` | `vbt::backtest` | 4 | 4 |
| `vbt.backtest.gate_vector` | `vbt::backtest` | 5 | 5 |
| `vbt.backtest.panel_from_frame` | `vbt::backtest` | 5 | 5 |
| `vbt.backtest.run_backtest` | `vbt::backtest` | 14 | 14 |
| `vbt.backtest.summarise` | `vbt::backtest` | 9 | 9 |
| `vbt.backtest.yearly` | `vbt::backtest` | 5 | 5 |
| `vbt.breadth.breadth_above_dma` | `vbt::breadth` | 8 | 8 |
| `vbt.breadth.breadth_series` | `vbt::breadth` | 4 | 4 |
| `vbt.breadth.gate_for` | `vbt::breadth` | 7 | 7 |
| `vbt.calendar.SessionCalendar.advance` | `vbt::calendar` | 5 | 5 |
| `vbt.calendar.SessionCalendar.index_of` | `vbt::calendar` | 6 | 6 |
| `vbt.calendar.SessionCalendar.sessions_between` | `vbt::calendar` | 5 | 5 |
| `vbt.calendar.build_calendar` | `vbt::calendar` | 5 | 5 |
| `vbt.calendar.drop_thin_sessions` | `vbt::calendar` | 4 | 4 |
| `vbt.calendar.session_counts` | `vbt::calendar` | 5 | 5 |
| `vbt.calendar.thin_sessions` | `vbt::calendar` | 8 | 8 |
| `vbt.config.DataConfig.min_samples` | `vbt::config` | 11 | 11 |
| `vbt.config.VbtConfig.bars_required` | `vbt::config` | 5 | 5 |
| `vbt.config.filter_legend` | `vbt::config` | 4 | 4 |
| `vbt.drift.compare` | `vbt::drift` | 7 | 7 |
| `vbt.exits.apply_stop` | `vbt::exits` | 4 | 4 |
| `vbt.exits.initial_stop` | `vbt::exits` | 6 | 6 |
| `vbt.exits.manage` | `vbt::exits` | 12 | 12 |
| `vbt.exits.r_multiple` | `vbt::exits` | 6 | 6 |
| `vbt.exits.stop_fill` | `vbt::exits` | 7 | 7 |
| `vbt.exits.tick_floor` | `vbt::exits` | 6 | 6 |
| `vbt.indicators.densify` | `vbt::indicators` | 5 | 5 |
| `vbt.indicators.require_columns` | `vbt::indicators` | 3 | 3 |
| `vbt.indicators.with_vbt_indicators` | `vbt::indicators` | 7 | 7 |
| `vbt.orders.advance_session` | `vbt::orders` | 3 | 3 |
| `vbt.orders.apply_fill` | `vbt::orders` | 4 | 4 |
| `vbt.orders.expire_orders` | `vbt::orders` | 5 | 5 |
| `vbt.orders.expires_after` | `vbt::orders` | 7 | 7 |
| `vbt.orders.fill_if_touched` | `vbt::orders` | 11 | 11 |
| `vbt.orders.is_expired` | `vbt::orders` | 6 | 6 |
| `vbt.orders.sessions_since` | `vbt::orders` | 5 | 5 |
| `vbt.plan.BookState` | `vbt::plan` | 5 | 5 |
| `vbt.plan.assemble` | `vbt::plan` | 5 | 5 |
| `vbt.plan.build_entries` | `vbt::plan` | 17 | 17 |
| `vbt.plan.exit_lines` | `vbt::plan` | 10 | 10 |
| `vbt.plan.to_tick` | `vbt::plan` | 8 | 8 |
| `vbt.published.PUBLISHED` | `vbt::published` | 1 | 1 |
| `vbt.signals.chartink_scan` | `vbt::signals` | 9 | 9 |
| `vbt.signals.detect_signals` | `vbt::signals` | 6 | 6 |
| `vbt.signals.signal_mask` | `vbt::signals` | 3 | 3 |
| `vbt.signals.trend_filters` | `vbt::signals` | 7 | 7 |
| `vbt.signals.with_signal_columns` | `vbt::signals` | 4 | 4 |
| `vbt.sizing.first_live_multiplier` | `vbt::sizing` | 6 | 6 |
| `vbt.sizing.size_entry` | `vbt::sizing` | 15 | 15 |
| `vbt.sleeve.sleeve_value` | `vbt::sleeve` | 7 | 7 |
| **total** | | **326** | **326** |

**`L1/fno` — 615 goldens**

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `fno.calendar.entry_session` | `fno::calendar` | 3 | 3 |
| `fno.calendar.f2_contract_expiry` | `fno::calendar` | 5 | 5 |
| `fno.calendar.hard_exit_at` | `fno::calendar` | 3 | 3 |
| `fno.calendar.hard_exit_session` | `fno::calendar` | 4 | 4 |
| `fno.calendar.held_expiry` | `fno::calendar` | 3 | 3 |
| `fno.calendar.in_entry_window` | `fno::calendar` | 1 | 1 |
| `fno.calendar.is_entry_day` | `fno::calendar` | 3 | 3 |
| `fno.calendar.is_late_exit` | `fno::calendar` | 3 | 3 |
| `fno.calendar.monthly_expiries` | `fno::calendar` | 3 | 3 |
| `fno.calendar.monthly_expiry` | `fno::calendar` | 3 | 3 |
| `fno.calendar.next_entry` | `fno::calendar` | 5 | 5 |
| `fno.calendar.plan_expires_at` | `fno::calendar` | 4 | 4 |
| `fno.calendar.roll_session` | `fno::calendar` | 3 | 3 |
| `fno.calendar.sessions_before` | `fno::calendar` | 5 | 5 |
| `fno.calendar.sessions_between` | `fno::calendar` | 3 | 3 |
| `fno.checklist.into_expiry` | `fno::checklist` | 5 | 5 |
| `fno.checklist.journal_gap` | `fno::checklist` | 2 | 2 |
| `fno.checklist.mark_gaps` | `fno::checklist` | 2 | 2 |
| `fno.checklist.missed_cycles` | `fno::checklist` | 1 | 1 |
| `fno.checklist.paper_checklist` | `fno::checklist` | 7 | 7 |
| `fno.checklist.uncovered_steps` | `fno::checklist` | 5 | 5 |
| `fno.condor.LegQuote` | `fno::condor` | 1 | 1 |
| `fno.condor.cost_to_close` | `fno::condor` | 5 | 5 |
| `fno.condor.credit` | `fno::condor` | 4 | 4 |
| `fno.condor.levels` | `fno::condor` | 7 | 7 |
| `fno.condor.liquidity_refusals` | `fno::condor` | 6 | 6 |
| `fno.condor.max_loss_per_unit` | `fno::condor` | 3 | 3 |
| `fno.condor.one_sd` | `fno::condor` | 6 | 6 |
| `fno.condor.price_structure` | `fno::condor` | 5 | 5 |
| `fno.condor.roles` | `fno::condor` | 1 | 1 |
| `fno.condor.select_strikes` | `fno::condor` | 8 | 8 |
| `fno.config.FnoConfig.risk_per_trade_pct` | `fno::config` | 2 | 2 |
| `fno.config.ceiling_violations` | `fno::config` | 5 | 5 |
| `fno.config.construct` | `fno::config` | 19 | 19 |
| `fno.config.f1_sleeve_for` | `fno::config` | 4 | 4 |
| `fno.config.f3_sleeve_for` | `fno::config` | 3 | 3 |
| `fno.config.group_of` | `fno::config` | 1 | 1 |
| `fno.costs.condor_fills` | `fno::costs` | 2 | 2 |
| `fno.costs.condor_round_trip` | `fno::costs` | 7 | 7 |
| `fno.costs.crossing_cost` | `fno::costs` | 2 | 2 |
| `fno.costs.future_charges` | `fno::costs` | 5 | 5 |
| `fno.costs.future_roll` | `fno::costs` | 2 | 2 |
| `fno.costs.future_round_trip` | `fno::costs` | 3 | 3 |
| `fno.covered.assess_step` | `fno::covered` | 5 | 5 |
| `fno.covered.book_after` | `fno::covered` | 1 | 1 |
| `fno.covered.net_of_plan` | `fno::covered` | 4 | 4 |
| `fno.covered.uncovered` | `fno::covered` | 12 | 12 |
| `fno.directional.add_decision` | `fno::directional` | 11 | 11 |
| `fno.directional.bars_needed` | `fno::directional` | 2 | 2 |
| `fno.directional.choose_expiry` | `fno::directional` | 6 | 6 |
| `fno.directional.confirm_75` | `fno::directional` | 6 | 6 |
| `fno.directional.daily_direction` | `fno::directional` | 6 | 6 |
| `fno.directional.direction_for` | `fno::directional` | 3 | 3 |
| `fno.directional.f3_exit` | `fno::directional` | 10 | 10 |
| `fno.directional.f3_marks` | `fno::directional` | 1 | 1 |
| `fno.directional.intraday_aligned` | `fno::directional` | 1 | 1 |
| `fno.directional.key_level` | `fno::directional` | 1 | 1 |
| `fno.directional.level_broken` | `fno::directional` | 2 | 2 |
| `fno.directional.levels` | `fno::directional` | 6 | 6 |
| `fno.directional.pivots` | `fno::directional` | 5 | 5 |
| `fno.directional.propose_spread` | `fno::directional` | 10 | 10 |
| `fno.directional.seventy_five_minute_bars` | `fno::directional` | 5 | 5 |
| `fno.directional.size_entry` | `fno::directional` | 6 | 6 |
| `fno.directional.spread_arith` | `fno::directional` | 3 | 3 |
| `fno.directional.spread_strikes` | `fno::directional` | 7 | 7 |
| `fno.directional_retest.choose_expiry` | `fno::directional_retest` | 3 | 3 |
| `fno.directional_retest.constants` | `fno::directional_retest` | 1 | 1 |
| `fno.directional_retest.index_panel` | `fno::directional_retest` | 2 | 2 |
| `fno.directional_retest.monthly_expiries` | `fno::directional_retest` | 2 | 2 |
| `fno.directional_retest.run_f3_retest` | `fno::directional_retest` | 2 | 2 |
| `fno.directional_retest.run_symbol` | `fno::directional_retest` | 9 | 9 |
| `fno.directional_retest.sessions_left` | `fno::directional_retest` | 1 | 1 |
| `fno.directional_retest.strike_step` | `fno::directional_retest` | 4 | 4 |
| `fno.exits.book_paused` | `fno::exits` | 4 | 4 |
| `fno.exits.f1_exit` | `fno::exits` | 10 | 10 |
| `fno.exits.f1_paused` | `fno::exits` | 7 | 7 |
| `fno.exits.f2_exit` | `fno::exits` | 11 | 11 |
| `fno.exits.f2_initial_stop` | `fno::exits` | 3 | 3 |
| `fno.exits.f2_month_paused` | `fno::exits` | 4 | 4 |
| `fno.exits.f2_time_exit_session` | `fno::exits` | 5 | 5 |
| `fno.exits.f2_trail` | `fno::exits` | 4 | 4 |
| `fno.exits.gtt_trigger` | `fno::exits` | 3 | 3 |
| `fno.exits.stop_from_vol` | `fno::exits` | 2 | 2 |
| `fno.gating.FnoFlags.execution_enabled` | `fno::gating` | 4 | 4 |
| `fno.gating.constants` | `fno::gating` | 1 | 1 |
| `fno.gating.fno_gates` | `fno::gating` | 24 | 24 |
| `fno.journal.future_costs` | `fno::journal` | 2 | 2 |
| `fno.journal.journal_figures` | `fno::journal` | 6 | 6 |
| `fno.journal.one_pool` | `fno::journal` | 4 | 4 |
| `fno.journal.option_costs` | `fno::journal` | 2 | 2 |
| `fno.journal.r_multiple` | `fno::journal` | 6 | 6 |
| `fno.ledger.book_pause_limit` | `fno::ledger` | 4 | 4 |
| `fno.ledger.build_ledger` | `fno::ledger` | 3 | 3 |
| `fno.ledger.column_pause_applies` | `fno::ledger` | 1 | 1 |
| `fno.ledger.evaluate_pauses` | `fno::ledger` | 10 | 10 |
| `fno.ledger.figures` | `fno::ledger` | 7 | 7 |
| `fno.ledger.mode_tag` | `fno::ledger` | 1 | 1 |
| `fno.ledger.month_bounds` | `fno::ledger` | 1 | 1 |
| `fno.ledger.pauses_for` | `fno::ledger` | 2 | 2 |
| `fno.monitor.f1_close_cost` | `fno::monitor` | 2 | 2 |
| `fno.monitor.f1_pnl_inr` | `fno::monitor` | 3 | 3 |
| `fno.monitor.f2_action` | `fno::monitor` | 8 | 8 |
| `fno.monitor.f2_pnl_inr` | `fno::monitor` | 2 | 2 |
| `fno.monitor.f2_trail_step` | `fno::monitor` | 5 | 5 |
| `fno.monitor.reprice_condor` | `fno::monitor` | 9 | 9 |
| `fno.monitor.reprice_future` | `fno::monitor` | 10 | 10 |
| `fno.research.b76` | `fno::research` | 2 | 2 |
| `fno.research.basis_study` | `fno::research` | 3 | 3 |
| `fno.research.build_option_panel` | `fno::research` | 1 | 1 |
| `fno.research.condor_trades` | `fno::research` | 7 | 7 |
| `fno.research.constants` | `fno::research` | 1 | 1 |
| `fno.research.continuous` | `fno::research` | 3 | 3 |
| `fno.research.debit_trades` | `fno::research` | 1 | 1 |
| `fno.research.f2_trades` | `fno::research` | 1 | 1 |
| `fno.research.futures_panel` | `fno::research` | 1 | 1 |
| `fno.research.futures_signals` | `fno::research` | 1 | 1 |
| `fno.research.implied` | `fno::research` | 2 | 2 |
| `fno.research.leg_costs` | `fno::research` | 2 | 2 |
| `fno.research.liquid_base` | `fno::research` | 1 | 1 |
| `fno.research.liquid_symbols` | `fno::research` | 1 | 1 |
| `fno.research.simulate_futures` | `fno::research` | 2 | 2 |
| `fno.research.summarise` | `fno::research` | 5 | 5 |
| `fno.retest.constants` | `fno::retest` | 1 | 1 |
| `fno.retest.empty_options` | `fno::retest` | 1 | 1 |
| `fno.retest.family_slippage` | `fno::retest` | 6 | 6 |
| `fno.retest.panels_from_contracts` | `fno::retest` | 2 | 2 |
| `fno.retest.retest_due` | `fno::retest` | 1 | 1 |
| `fno.retest.run_family` | `fno::retest` | 6 | 6 |
| `fno.retest.trim_index_contracts` | `fno::retest` | 2 | 2 |
| `fno.scan.allocate_capacity` | `fno::scan` | 7 | 7 |
| `fno.scan.f2_signals` | `fno::scan` | 7 | 7 |
| `fno.scan.propose_condor` | `fno::scan` | 10 | 10 |
| `fno.scan.propose_future` | `fno::scan` | 6 | 6 |
| `fno.series.continuous_futures` | `fno::series` | 7 | 7 |
| `fno.sizing.margin_check` | `fno::sizing` | 4 | 4 |
| `fno.sizing.risk_budget` | `fno::sizing` | 6 | 6 |
| `fno.sizing.size` | `fno::sizing` | 14 | 14 |
| `fno.underlying.derive_underlying` | `fno::underlying` | 7 | 7 |
| `fno.vol.atm_iv` | `fno::vol` | 6 | 6 |
| `fno.vol.iv_atm_frame` | `fno::vol` | 3 | 3 |
| `fno.vol.iv_expiry` | `fno::vol` | 4 | 4 |
| **total** | | **615** | **615** |

**What is still not exact (no golden is red because of it; DECISIONS-RS R3.x).**

* **twt: a literal NaN in an input bar frame** is compared with IEEE semantics in Rust and in
  polars' total order in Python (NaN largest, NaN == NaN; `max_horizontal` folds like `maxsd`;
  group `min` skips NaN) — `signals`, `breadth`, `indicators`, `liquidity_floor`,
  `limit_locked`, `month_low_back`, `tight_state`. Verified live (`close = NaN`, `sma_dma = 1.0`:
  polars counts the name above, Rust does not). Unreachable from real data (Postgres numeric →
  Float64 has no NaN; the only derived NaN, `tight_range_pct = 0/0`, compares the same in both),
  and the goldens cannot encode a NaN input. Documented as a precondition, to be closed before
  the TWT plan strangles its Python twin (R3.1). vbt and fno already port the total order.
* twt: `SessionCalendar::advance` with a step below `-len` returns `None` where Python raises
  `IndexError` (no caller passes a negative step); `size_entry` refuses a share count above
  `i64::MAX` where Python returns a big int (R3.3).
* vbt: config floats parsed with Rust's `{x}` rather than Python's `repr` in `fill_if_touched`
  and `size_entry` — compared or floored, never printed; a ~1e300 `fill_through_pct` could fail
  to parse where Python returns `None` (R3.6).
* fno: an exponent-form capital (`"1E+7"`) prints `risk_budget_inr` at another scale
  (`100000.0` vs `100000`; 28 of 600 fuzz cases) — `rust_decimal` has no positive exponent;
  `condor.one_sd` at `iv = 1e-12` raises `InvalidOperation` where Python returns a scale-31
  value (36 fuzz cases). Both R1.3's class; fail loudly or differ only in printed scale (R3.8).
* fno research: unreachable deviations — `leg_costs` with `lot = 0`, `simulate_futures` with an
  entry of 0, negative horizons, `trading_days_before(n < 0)`, polars' unstable sort on A2 ties
  (matched where polars falls back to its stable small-slice sort), and the fdlibm `erf` vs
  Apple libm (1–2 ulp) (R3.9).
* **Duplicated code (R2.3's pattern, to collapse):** twt carries private copies of VBT's config
  shape (`twt::config::vbt_shared`), calendar, breadth and indicator arithmetic, written before
  the vbt lane landed; `fno::ledger` carries private copies of `fno::exits`' pause predicates;
  `fno::costs` takes role→price maps instead of `fno::condor::LegRole`. Each now has a real
  home in the crate (R3.4).

## Wave 4, step 1 — `baskfy-providers` (foundation RS4.0; adapters RS4a, 29 Oct 2026)

The I/O stack is pinned once for every wave-4 porter (DECISIONS-RS R4.1, R4.2). The crate has no
goldens: Python's own provider tests are ported case for case, plus cross-checks against the live
Python (messages, formatting, the Lua scripts, a Python-written token blob).

| Python module | Rust | state | tests |
|---|---|---|---|
| `errors` | `errors` (R4.3) | ported | 3 unit |
| `records` | `records` (R4.6) | ported | `records_and_ports.rs` 24 (all of `test_records_and_ports.py` that does not need kite/nse/fixtures, plus holdings and timestamps) |
| `ports` | `ports` (async traits) | ported | in `records_and_ports.rs` |
| `settings` | `settings` (R4.7) | ported | `settings.rs` 8 |
| `ratelimit` | `ratelimit` (R4.8) | ported | `ratelimit.rs` 19 (every `test_ratelimit.py` case + spacer, lanes, script identity, opt-in real Redis) |
| `retry` | `retry` | ported | `retry_and_tokens.rs` (every case) |
| `circuit` | `circuit` | ported | `circuit_and_publication.rs` 11 (the `CircuitBreaker` cases of `test_composite.py`) |
| `tokens` | `tokens` (R4.9) | ported | `retry_and_tokens.rs` 30 in all (every case + Python interop) |
| `publication` | `publication` | ported | in `circuit_and_publication.rs` |
| `reference_export_io` | `reference_export_io` | **path half only** — `reference_rows()` waits on core's `reference_export` (`read_export`/`to_rows`, not ported) | — |
| `network_guard.py` (suite) | `net` (R4.5) | ported | `network_is_blocked.rs` 13 (Python's cases + two source scans) |
| (none) | `clock`, `secret`, `py` | new seams | 6 unit |
| `kite` (+ kiteconnect 5.2.1's read-only REST half) | `kite` (R4.10) | ported (RS4a) | `kite_provider.rs` 58 (incl. ~140 live-Python coercions, 24 SDK CSV bodies), `kite_http.rs` 24, `kite_options.rs` 16, `kite_holdings_port.rs` 5, `kite_fno_master.rs` 3, `kite_timeout.rs` 3 |
| `nse` | `nse` (R4.11) | ported (RS4a) | `nse_provider.rs` 71 (all 58 `test_nse.py` cases, plus review additions), `nse_catalyst.rs` 21, `nse_fo_ban_list.rs` 12, `nse_fo_bhavcopy.rs` 7, `nse_http_wire.rs` 5; 229-file differential: 215 identical, the rest R4.11's documented deviations |
| `archive` | `archive` (R4.14) | ported (RS4a) | `nse_archive.rs` 22 (all 19 `test_archive.py` cases, plus review additions) |
| `composite` | `composite` | ported (RS4a) | `nse_composite.rs` 25 (+ breaker cases in `circuit_and_publication.rs`) |
| `fixtures` | `fixtures` (R4.12) | ported, **in-memory frames only** | `nse_fixtures.rs` 27 + 8 ignored (file-backed, need `parquet`); `TestAnchoredToRealData` (4) and `TestDeterminism` (1) have no counterpart yet |
| `factory` | `factory` (R4.13) | ported (RS4a), production wiring with Kite | `nse_factory.rs` 17 |
| `fixture_builder`, `cli` | — | not started | not declared yet |

**RS4a residuals (open, all recorded in DECISIONS-RS):**

* **Parquet (R4.12):** `FixtureProvider` cannot read its files, so docs/03's `local` environment
  does not run on the Rust stack yet; 8 file-backed cases are ignored until the feature is declared.
* **S3 retries (R4.14):** `SigV4S3Client` has botocore's 60 s timeouts but none of boto3's
  retries; one transient R2 error fails archive-then-parse. Must close before the worker's nightly
  runs on Rust.
* **Kite known bug, reproduced (R4.10):** transport timeouts/refusals are untranslated and not
  retried, in both languages; fix both together later.
* **Kite `check()` (R4.10):** every token-store error becomes a health string; Python lets an
  `OSError` raise. Needs a `HealthReporting` trait change.
* **`records::conform` (R4.10, R4.11h):** panics on a current-thread tokio runtime (polars' lazy
  `block_in_place`), and maps every polars failure to `InvalidOperationError` (Python:
  `ComputeError` for a precision overflow). Foundation-owner items; adapters avoid `conform`.
* **NSE deviations (R4.11 a–g):** error classes/texts on refused values, R1.3-class limits,
  `1E+5` printing, Unicode digits, redirect cap 10 (httpx 20), `raw` key order — none changes a
  stored value.
* **`reference_export_io.reference_rows()`** still waits on core's `reference_export`.
* **Secret audit (R4.15):** clean; the Kite `Authorization` header is now sensitive and an
  unparseable Redis URL is never quoted.

**For the kite and nse porters (they run in parallel and own one file each):** never edit
`Cargo.toml` or `lib.rs`; build HTTP clients only from `net::client_builder()` /
`net::GuardedClient`; begin every test with `common::arm()` (`tests/common/mod.rs`; a scan fails
otherwise); take rates and keys from `ProviderSettings` and the factory constants, never a literal.
Python imports that have no Rust home yet: `baskfy_core.tradebook` (`TradeFill`, `TradeSide`, used by
kite.py) and `baskfy_core.universes.UNIVERSE_BY_SLUG` (nse.py; the screener has a private copy,
R2.3) — port privately per R2.3 or ask the core lead.

## Wave 4b foundation (RS4b, 29–30 Oct 2026) — `baskfy-db`, `baskfy-api`, the API harness

**`baskfy-db`** (`rust/crates/baskfy-db`): one lazily-connected sqlx pool sized exactly as
Python's `create_engine` (10 + 10, 5 s checkout, 1800 s recycle, pre-ping, statement cache 100;
no statement timeout, as Python sets none — R5.2), **read-only on every connection**
(`default_transaction_read_only=on` in the startup packet; `tests/bed.rs::a_write_is_refused`
proves `UPDATE` and `CREATE TABLE` fail with SQLSTATE 25006), and typed row helpers: `numeric`
decoded from the wire with asyncpg's scale (`0::numeric(10,2)` is `"0.00"`; sqlx's own decoder
says `"0"` — R5.3), `timestamptz` as UTC, `date`, and `jsonb` as `OrderedJson` (Python's key order,
Python's int/float split). Never migrates; Alembic owns the schema.

**`baskfy-api`** (`rust/crates/baskfy-api`, binary `baskfy-api --host H --port P`):

| Piece | Python | State |
|---|---|---|
| settings, 160 `BASKFY_*` fields, defaults, coercion, constraints, `require_configured` | `settings.py` | ported; field list held equal to Python's by a fixture |
| problem+json, every type/status/title, extension members in Python's order | `problems.py`, `app.register_error_handlers` | ported |
| validation errors (`400 invalid-screen-definition`, `errors[] {field, message, type}`), pydantic's date messages | `app._handle_validation` | ported for query parameters (`query.rs`); body validation arrives with the first write route |
| request id, access log, JSON/text logging with redaction | `logging.py`, `app._request_context` | ported |
| CORS (Starlette `CORSMiddleware`, hand-ported) | `app.register_middleware` | ported |
| ETag / Cache-Control / Vary / 304 | `http_cache.py` | ported |
| Starlette routing: decoded path, slash redirect (307), 404/405 problems, HEAD = 405 | Starlette `Router` | ported (R5.8) |
| bearer JWT (PyJWT 2.13's rules), lifetime cap, unknown subject, **session epoch** | `auth.py` | ported (R5.5) |
| principal + rate limit as the `/api/v1` router dependency (Python's Lua bucket, same keys) | `ratelimit.py` | ported; fails closed (503) without Redis |
| entitlements (plan → overrides → the seven keys) | `entitlements.py`, `baskfy_core.entitlements` | ported |
| CSRF double-submit check | `csrf.py` | check ported; cookie writers come with `/auth/refresh` |
| `GET /health` | `app.py` | ported |
| `GET /api/v1/me` | `routers/auth.py` | ported (R5.7: the staff-allowlist write is not performed) |
| `GET /meta/factors`, `/meta/columns`, `/meta/trading-days` | `routers/meta.py` | ported |
| `GET /meta/universes`, `/meta/ranking-presets`, `/meta/status`, `/meta/live-marks` | `routers/meta.py` | ported by the reference lane (below; `live_quotes` on the no-session path only, R5.11) |
| `GET /metrics`, `/api/v1/openapi.json`, `/api/v1/docs` | `app.py`, FastAPI | **not started** |
| every other router's GET routes | `routers/*.py` | ported by the five lanes (below) |

**Tests ported:** `test_api_errors.py` (problem documents, authentication, request ids, rate
limiting; `TestPipelineDegraded` waits for `/meta/status`), `test_api_auth.py`'s GET paths
(`TestMe` read half, the token half of `TestSecurity`, the read half of `TestSessionEpoch`),
`test_bad_request_problem.py`, `test_http_cache.py` (pure half and the wired half on
`/meta/columns`), `test_log_redaction.py::TestTheFilter`, plus PyJWT's acceptance rules case by
case (`tests/jwt.rs`, each checked against PyJWT 2.13). Not applicable yet: `test_security.py`
(Argon2id/OTP/opaque tokens — no route that uses them is ported), `test_audit_auth_gates.py`
(desk/baskets/kite routes not ported; their lanes inherit the cases).

**Differential harness:** `tools/parity/api_diff.py` (README §"Grading the API") over
`tools/parity/api_cases/foundation.json` — 55 cases, **55 EQUAL**: health, framework 404/405/307,
decoded paths, HEAD, the three meta routes (incl. 304 revalidation), every token failure mode,
`/me` for three real accounts plus anonymous and a stale-epoch token, CORS preflight and simple
responses. The Python API boots against the bed with test settings only; its startup writes
nothing (the bed's `pg_stat_user_tables` insert/update/delete total was unchanged across a full
run).

## API lanes (RS4b, 30 Oct 2026) — every GET route but one, 2,113 of 2,113 cases EQUAL

Five lanes (reference, sleeves, derivatives, portfolio, accounts) ported the GET routes of the
44 Python routers. Each lane then had an adversarial verifier, which fixed what it found and grew
the case files. The integrator merged them (R5.10).

**Grading.** `python3 tools/parity/api_diff.py --py-port 18001 --rs-port 18002 --env
BASKFY_SOLE_USER_ID=1 --env BASKFY_FIXTURE_DIR=<an empty dir>` runs all six case files, 2,113
cases, and gives **2,113 of 2,113 EQUAL**. Without `BASKFY_SOLE_USER_ID` (the 503 branch) it also
gives **2,113 of 2,113 EQUAL**. Per case file: foundation 55, reference 810, sleeves 219,
derivatives 230, portfolio 315, accounts 484.

**Routes.** Python's routers declare 147 GET routes; 7 of them are `public` (never mounted, D9).
Rust registers **139 of the 140 mounted router GETs**, plus `/health`. The one missing route is
`GET /overlap` (R5.21). Not ported from `app.py`: `/metrics`, `/api/v1/openapi.json` and
`/api/v1/docs`. Python's routers also declare **94 write routes** (60 POST, 16 DELETE, 11 PATCH,
7 PUT); **none is registered** in Rust (the pool is read-only, R5.2). They are wave 4c.

In the table below, cases are attributed to the router whose Python route they hit; framework
cases are unrouted paths, the root and decoded-path probes.

| Router | Lane | GET ported / total | Diff cases EQUAL / total | Write routes (wave 4c) |
|---|---|---:|---:|---:|
| `admin` | accounts | 9 / 9 | 93 / 93 | 5 |
| `alerts` | accounts | 2 / 2 | 22 / 22 | 4 |
| `api_keys` | accounts | 2 / 2 | 34 / 34 | 4 |
| `auth` | foundation + accounts | 2 / 2 | 25 / 25 | 6 |
| `backtests` | derivatives | 7 / 7 | 72 / 72 | 2 |
| `baskets` | sleeves | 2 / 2 | 12 / 12 | 0 |
| `billing` | portfolio | 3 / 3 | 40 / 40 | 2 |
| `brokers` | portfolio | 3 / 3 | 33 / 33 | 2 |
| `curated_costs` | accounts | 1 / 1 | 23 / 23 | 0 |
| `curated_create` | accounts | — (no GET) | 0 / 0 | 1 |
| `curated_customize` | accounts | — (no GET) | 0 / 0 | 1 |
| `curated_drift` | accounts | — (no GET) | 0 / 0 | 2 |
| `curated_engage` | accounts | 2 / 2 | 11 / 11 | 2 |
| `curated_from_screen` | accounts | — (no GET) | 0 / 0 | 2 |
| `curated_investments` | accounts | 3 / 3 | 67 / 67 | 3 |
| `curated_plans` | accounts | — (no GET) | 0 / 0 | 3 |
| `curated_sip` | accounts | 1 / 1 | 26 / 26 | 1 |
| `curated_trending` | accounts | 1 / 1 | 6 / 6 | 0 |
| `curated_versions` | accounts | 2 / 2 | 33 / 33 | 0 |
| `desk` | reference | 5 / 5 | 51 / 51 | 0 |
| `explore` | reference | 9 / 9 | 218 / 218 | 2 |
| `fno` | derivatives | 3 / 3 | 16 / 16 | 1 |
| `instruments` | reference | 6 / 6 | 180 / 180 | 0 |
| `kite` | reference | 2 / 2 | 96 / 96 | 0 |
| `managers` | accounts | 2 / 2 | 16 / 16 | 4 |
| `market_data` | reference | 4 / 4 | 152 / 152 | 0 |
| `meta` | reference + foundation | 7 / 7 | 55 / 55 | 0 |
| `options` | derivatives | 9 / 9 | 124 / 124 | 3 |
| `overlap` | derivatives | 3 / 4 — `GET /overlap` not ported (R5.21) | 17 / 17 | 5 |
| `portfolio_overview` | portfolio | 7 / 7 | 118 / 118 | 3 |
| `portfolios` | portfolio | 6 / 6 | 80 / 80 | 6 |
| `public` | reference | 0 / 7 — never mounted (D9) | 6 / 6 | 0 |
| `screens` | accounts | 4 / 4 | 108 / 108 | 8 |
| `search` | reference | 1 / 1 | 77 / 77 | 0 |
| `sleeve_state` | sleeves | 1 / 1 | 9 / 9 | 0 |
| `sleeves` | sleeves | 2 / 2 | 25 / 25 | 1 |
| `support` | — | — (no GET) | 0 / 0 | 1 |
| `swing` | sleeves | 10 / 10 | 113 / 113 | 5 |
| `track_b` | portfolio | 3 / 3 | 16 / 16 | 3 |
| `trades` | portfolio | 1 / 1 | 27 / 27 | 2 |
| `twt` | sleeves | 3 / 3 | 19 / 19 | 1 |
| `vbt` | sleeves | 7 / 7 | 41 / 41 | 2 |
| `watchlist` | accounts | 2 / 2 | 16 / 16 | 3 |
| `webhook_endpoints` | accounts | 2 / 2 | 21 / 21 | 4 |
| framework + `/health` (`app.py`) | foundation | 1 / 1 | 15 / 15 | — |
| **total** | | **140 / 141** mounted (+7 public, never mounted) | **2113 / 2113** | **94** |

**Audit at integration (30 Oct 2026):**
* **No write and no order route is registered.** A source scan of every `.route(` in
  `baskfy-api/src` finds 139 `get(...)` routes plus 14 empty `MethodRouter`s, the portfolio
  lane's write-only paths, which answer 405 (R5.15). No `post`/`put`/`patch`/`delete`, and no
  `place_order`/`modify_order`/`cancel_order`/`baskfy_execution` outside the tests that assert
  their absence.
* **The pool is read-only on every connection.** `baskfy_db::pool::READ_ONLY` is `true` and is sent
  as `default_transaction_read_only=on` in the startup packet. No other pool is constructed, and no
  SQL `INSERT`/`UPDATE`/`DELETE`/`SET ... READ WRITE` exists in the API.
* **The public API is shut.** `routes::public` is never merged and its `router()` is empty; six
  cases grade the 404s.
* **No secrets in logs.** `Secret` redacts in `Debug`. The harness's server logs from both runs
  were scanned for the JWT secret, any bearer token and the bed password: none found.

**Cutover gates** (a route moves to Rust only when its gate is clear):
* the live Kite overlay (R5.11);
* `/baskets` without a snapshot (R5.17);
* `/brokers/callback` (R5.27);
* `/backtests/{id}/events` while a run is in flight (R5.22);
* `GET /overlap` (R5.21);
* every non-GET method (R5.15).

R5.36 lists the residual differences no committed case reaches. R5.28 lists two **Python-side
defects** the port reproduces on purpose (the `/trades` cross-tenant date leak and the
unauthenticated rank-history read), to be fixed in Python first.

**Tests:** `cargo test --workspace` with the bed URL: 61 targets, **928 passed, 0 failed,
14 ignored** (the same 14 as RS4a). `baskfy-api` integration targets: `api_errors`, `jwt`,
`pipeline`, `settings`, `reference` (19), `derivatives` (10), `portfolio` (12), plus the lanes'
unit tests and the accounts bed HTTP tests in the lib.

### Lane: derivatives — detail

Every GET of the four routers except one, graded by `tools/parity/api_diff.py` over
`tools/parity/api_cases/derivatives.json`: **230 of 230 EQUAL** (176 from the porter, 54 from the verifier) with `--env BASKFY_SOLE_USER_ID=1`
(the bed's owner holds every options/FO row), and 230 of 230 without it (the 503 branch). Cases
cover each route's parameters (dates, lax ints, bounds, `Literal`s, last-value-wins, blanks),
the sole tenant and two other accounts, anonymous and bad tokens, 404s, 402s, 307s.

| Route | Python | State |
|---|---|---|
| `GET /options/{today,scan/{sleeve},chain,positions,sessions,journal,backtest,calendar,config}` | `routers/options.py`, `options_read`, `options_settings` | ported (9 routes) |
| `GET /fno/{overnight,info,config}` | `routers/fno.py`, `fno_read`, `fno_settings` | ported (3 routes) |
| `GET /backtests`, `/{id}`, `/{id}/trades`, `/{id}/holdings`, `/{id}/export`, `/{id}/download/{artefact}`, `/{id}/events` | `routers/backtests.py`, `backtests` | ported (7 routes); `/events` of a run still in flight answers as Python's no-cache branch (R5.22) |
| `GET /overlap/tags/export`, `/overlap/reviews/export`, `/overlap/catalyst-scan` | `routers/overlap.py`, `overlap`, `overlap_scan` | ported (3 routes) |
| `GET /overlap` | `overlap.overlap` + `candidate_review` + `catalyst_tags` + live marks | **not ported** (R5.21): Python serves it |
| every write of the four routers | — | **not registered** (R5.2): Python serves them |

**What the bed cannot grade** (no rows): open/closed FO and options positions and journals
(`fo_position`, `fo_journal`, `fo_mark`, `op_position`, `op_journal` are empty), options backtest
runs, a finished backtest's artefacts and signed download, the corrections/labels exports. Their
code follows Python line for line and the pure parts carry unit tests (`options_core`: roles,
the never-pooled summary; `backtests`: the frame, the signature, the config re-dump, the CSV
reader, the cursor), but no differential case has exercised them.

**Tests:** `crates/baskfy-api/tests/derivatives.rs` (10: anonymous refusals and 405s for every
path, no path names an order, no source names the execution package or a broker, the download
link's refusals before any read; on the bed the sole-tenant guard, validation order, the
entitlement gate, another account's 404, a failed run's artefacts and event stream) plus unit
tests in the modules. Private ports of Python core: R5.20.

## Wave 3 answer keys (RS2) — ported at RS3

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

Wave 3 is done (above). The golden-covered pure core is complete; **128 `baskfy_core` modules
with no goldens are still `not started`** (models, curated_*, portfolio_*, allocation_ledger,
reconcile, exposure, options' plan/scan/structures, swing.backtest, …). They are pulled in by
the I/O layer that first needs them, **goldens first** (a dumper in `tools/parity/`, committed
separately), never ported blind.

**Done at RS4a:** the wave-3 housekeeping below (R3.4, R3.1, the last R3.1 site in
`fno::series` closed by the RS4a integrator) and wave-4 step 1, `baskfy-providers` (above).
**Done at RS4b (foundation):** `baskfy-db` and the `baskfy-api` foundation with its harness
(above). **Done at RS4b (lanes):** every GET route but `GET /overlap`, 2,113 of 2,113 cases
EQUAL (above).

**Next — wave 4c, the write routes** (94 of them). Run them on a **fresh bed copy** for every
run, and compare the **database side effects** as well as the response: the rows each table
gains, changes and loses, with the same masks discipline as the bodies. Wave 4c also needs a
decision that lets the pool write (R5.2), one table at a time, idempotent (house rule 7). Before
it starts:
* collapse the lanes' duplicate helpers (R5.10);
* add the big-integer `jsonb` variant (R5.36.1);
* commit a fixture seed, so the paths the bed lacks are graded by committed cases (R5.30).

Never a route that places, modifies or cancels an order (law 2: execution is the last wave).
**Then the worker** (`baskfy-worker`, step 4 below).

**Housekeeping step (R3.4, R3.1) — done:** collapse the R2.3/R3.4 copies
(twt's `vbt_shared` config, calendar, breadth and indicator arithmetic onto `vbt::*`;
`fno::ledger`'s pause predicates onto `fno::exits`; the two `windows` copies), lift
`WideDecimal` out of `backtest::helpers` into a shared `baskfy_core` module, and give twt the
polars total-order comparisons vbt already has. Every golden must stay green through it.

**Wave 4 — the I/O layers, strictly in this order** (R3.10). Each gets its own crate, added by
the step that needs it; law 1 keeps every one of them out of `baskfy-core`.

1. **`baskfy-providers`** — Kite and NSE clients (historical, quotes, instruments, bhavcopy
   download/parse), rate-limited exactly as the Python's (Kite ~3 req/s historical; NSE's
   cookie/header discipline). Graded by **recorded-fixture goldens** (request → response pairs
   captured from the Python clients against recorded HTTP, never live), plus the rate limiter's
   timing as a test with an injected clock. No live network in any test.
2. **`baskfy-db`** — `sqlx` against the **existing** Postgres schema (Alembic stays the
   migration tool). **Read-only first**: every query the API and worker read with, typed rows
   whose `numeric` columns decode to `rust_decimal`, graded against a **copy** of the database
   (never the live `baskfy-local` containers) by comparing row sets with the Python's. Writes
   come later, one table at a time, each proven idempotent (house rule 7) on the copy.
3. **`baskfy-api`** (axum) — route for route against the FastAPI app: serves `openapi.json`
   unchanged, with **contract tests generated from that OpenAPI** and JSON bodies equal after
   normalisation (docs/go-rewrite/03 §Tolerances). The public API stays shut (D9). The web app
   never gains an execute route. Cut over one Caddy route at a time.
4. **`baskfy-worker`** — the Celery jobs, the nightly first (catch-up, publish, the quality
   gate); graded by `pipeline_run_step` rows equal to the Python run's against a DB copy. The
   two clocks (CLAUDE.md, "Which date the product shows") are preserved exactly: published
   series never claim today while today is running.
5. **`baskfy-desk`** — `kite-momentum-rebalancer/app`: plans, monitors, the sleeves' services;
   two shadow Fridays in `DRY_RUN` with byte-equal plans; cutover only on Maulik's explicit go.
   `portfolio.db` is never touched without the verified backup the safety rails require.
6. **`baskfy-execution` — LAST** (law 2): guards → risk → rate limit → journal → broker, the
   only path to an order. **Every non-negotiable's test is ported before any code**; `DRY_RUN`
   defaults true and must simulate end to end; `client_id = plan_id:symbol`; SGB*/G-sec and
   `EXCLUDED_SYMBOLS` refused before any network call; CNC-only with the MIS/NFO gates off by
   default; the three auto-execute flags (swing SW25/26, TWT TW17–20, F3 auto-exit M.5) ported
   with their defaults **false** and never widened. Never run against live credentials; no agent
   places a live order.

Then the single `baskfy` binary (`api`, `worker`, `desk` subcommands).

## `baskfy_core` — every Python module

Line counts are `wc -l` of the Python file (comments and docstrings included), measured
29 Oct 2026. Rust modules declared in `baskfy-core` today: `swing::{config, indicators, market,
opening_range, plan, setups, sizing, stops}`, `options::{condor, config, costs, execution,
gating, ledger}`, `backtest::{helpers, engine}`, `backtest_metrics`, `factor_registry`,
`factors`, `ranking_engine`, `screener` (RS2), and every module of `twt` (12), `vbt` (13) and
`fno` (20) (RS3). Python modules with no Rust module of their own
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
| `baskfy_core.fno.calendar` | 148 | ported — 51/51 |
| `baskfy_core.fno.checklist` | 284 | ported — 22/22 |
| `baskfy_core.fno.condor` | 274 | ported — 46/46 |
| `baskfy_core.fno.config` | 486 | ported — 34/34 |
| `baskfy_core.fno.costs` | 131 | ported — 21/21 |
| `baskfy_core.fno.covered` | 125 | ported — 22/22 |
| `baskfy_core.fno.directional` | 639 | ported — 91/91 |
| `baskfy_core.fno.directional_retest` | 495 | ported — 24/24 |
| `baskfy_core.fno.exits` | 167 | ported — 53/53 |
| `baskfy_core.fno.gating` | 106 | ported — 29/29 |
| `baskfy_core.fno.journal` | 279 | ported — 20/20 |
| `baskfy_core.fno.ledger` | 347 | ported — 29/29 |
| `baskfy_core.fno.monitor` | 312 | ported — 39/39 |
| `baskfy_core.fno.research` | 1021 | ported — 34/34 |
| `baskfy_core.fno.retest` | 612 | ported — 19/19 |
| `baskfy_core.fno.scan` | 448 | ported — 30/30 |
| `baskfy_core.fno.series` | 217 | ported — 7/7 |
| `baskfy_core.fno.sizing` | 144 | ported — 24/24 |
| `baskfy_core.fno.underlying` | 123 | ported — 7/7 |
| `baskfy_core.fno.vol` | 187 | ported — 13/13 |
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
| `baskfy_core.twt.backtest` | 756 | ported — 41/41 |
| `baskfy_core.twt.breadth` | 130 | ported — 21/21 |
| `baskfy_core.twt.calendar` | 95 | ported — 28/28 |
| `baskfy_core.twt.config` | 396 | ported (exercised through other goldens) |
| `baskfy_core.twt.drift` | 104 | ported — 8/8 |
| `baskfy_core.twt.exits` | 379 | ported — 74/74 |
| `baskfy_core.twt.indicators` | 184 | ported — 18/18 |
| `baskfy_core.twt.plan` | 550 | ported — 39/39 |
| `baskfy_core.twt.published` | 111 | ported — 1/1 |
| `baskfy_core.twt.signals` | 318 | ported — 48/48 |
| `baskfy_core.twt.sizing` | 184 | ported — 21/21 |
| `baskfy_core.twt.sleeve` | 148 | ported — 7/7 |
| `baskfy_core.universes` | 192 | not started |
| `baskfy_core.vbt` | 236 | not started |
| `baskfy_core.vbt.backtest` | 727 | ported — 42/42 |
| `baskfy_core.vbt.breadth` | 123 | ported — 19/19 |
| `baskfy_core.vbt.calendar` | 128 | ported — 38/38 |
| `baskfy_core.vbt.config` | 384 | ported — 20/20 |
| `baskfy_core.vbt.drift` | 78 | ported — 7/7 |
| `baskfy_core.vbt.exits` | 182 | ported — 41/41 |
| `baskfy_core.vbt.indicators` | 201 | ported — 15/15 |
| `baskfy_core.vbt.orders` | 196 | ported — 41/41 |
| `baskfy_core.vbt.plan` | 445 | ported — 45/45 |
| `baskfy_core.vbt.published` | 86 | ported — 1/1 |
| `baskfy_core.vbt.signals` | 138 | ported — 29/29 |
| `baskfy_core.vbt.sizing` | 161 | ported — 21/21 |
| `baskfy_core.vbt.sleeve` | 135 | ported — 7/7 |
| `baskfy_core.windows` | 211 | not started |

193 modules, 67550 lines in total. Goldens: 1,702 cases (swing 79, options 47, backtest 139,
ranking 190, twt 306, vbt 326, fno 615). Ported: 65 modules (RS3; twt 12, vbt 13 and fno 20 added),
**1,702 of 1,702** of their goldens green; 128 not started. Not yet marked `verified`: that
needs Maulik's review of the R1.x/R2.x/R3.x entries.
