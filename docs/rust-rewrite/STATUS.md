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
| API GET routes (RS4b) | **139 of 140** mounted router GETs; 2,285 of 2,285 cases EQUAL at RS4c |
| API write routes (RS4c) | **82 of 94 ported**, 1 partly, 11 on Python with reasons; 1,175 of 1,175 writes cases EQUAL with every side effect (section "Wave 4c integration") |
| `cargo test --workspace` at RS4c | **green**: 64 test targets, 1,013 passed + 14 ignored (the same 14); **1,702 of 1,702 graded goldens** |
| `baskfy-worker` foundation (RS5.0): steps, orchestrator, nightly task, settings, alerts, CLI, the replay provider, `nightly_diff.py` | **done** (RS5.0, section "Wave 5 foundation") |
| `baskfy-worker` nightly (RS5): all 14 steps | **ported**; `nightly_diff.py --chain` **EQUAL in 7 of 7 scenarios** py,rs; catch-up decision 11 of 11 EQUAL (section "Wave 5 integration") |
| `cargo test --workspace` at RS5.0 | **green**: 71 test targets, 1,038 passed + 14 ignored (the same 14); **1,702 of 1,702 graded goldens** |
| `baskfy-worker` tasks (RS6.0): registry (76), Beat (61), CLI `task`, `task_diff.py`, replay upstream | **foundation done**; 4 tasks wired and EQUAL py,rs, 72 declared stubs in 15 lanes (section "Wave 6 foundation") |
| Desk, execution, single binary | not started (the desk: its own workflow, `rs-desk`) |

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

## Wave 6 foundation (RS6.0, 30 Oct 2026) — the task registry, `task_diff.py`, the replay upstream

**Not committed** (the integrator commits). Decisions R8.1–R8.3, all ⚠ UNREVIEWED.

| Piece | Python | State |
|---|---|---|
| `tasks::registry` (76 tasks) | `celery_app` after `build_celery()` | **done**: names, Python functions, parameters (annotations, defaults), `bind`, `acks_late`, `autoretry_for`, backoff, `time_limit`, queue option; routing computed both ways; held equal to `tests/fixtures/python_task_registry.json` (`tools/parity/task_registry.py`) by `tests/task_registry.rs` (7 tests) |
| `tasks::beat` (61 entries) | `BEAT_SCHEDULE` | **done**: crontabs in IST as written and as Celery expands them, intervals, kwargs, queue/expires/countdown; `due_at`; `baskfy-worker beat --now` |
| `tasks::args` | CPython's call binding | **done**: CPython 3.12's order and messages (checked against the interpreter) |
| `tasks::context` | `dt.datetime.now(tz=IST)`, `build_pipeline_dependencies` | **done**: the injected, advancing clock; the replay seams; the dependency builder moved from `main.rs` |
| `tasks::pipeline` — `pipeline.nightly` (both paths), `pipeline.session_catch_up`, `compute.reprocess_instrument`, `pipeline.integrity_audit` | `celery_tasks.py` | **ported** (wave-5 code wired); `task_diff.py` **12 of 12 EQUAL py,rs** |
| 72 tasks in 15 lanes | `celery_tasks.py`, `desk.py` | **declared stubs** (`TaskNotPorted`, every one driven through the registry by a test) |
| CLI `task`, `tasks`, `beat` | Celery's worker | **done**; exit 0 / 75 (retryable) / 1 / 2 |
| `tools/parity/task_diff.py` + `task_py.py` | — | **done**: cases per lane, injected clock both sides, every effect compared (R8.2) |
| `tools/parity/replay_upstream.py` + the `session` bundle (`replay_build_session.py`) | — | **done** (R8.3); Kite historical minute candles not recorded yet |

**The lanes** (`rust/crates/baskfy-worker/src/tasks/<lane>.rs`, `tools/parity/task_cases/<lane>.json`):

| Lane | Tasks |
|---|---|
| `screener` | `compute.warm_screen_cache`, `alerts.dispatch` (both need the screener port, with R6.13 and R-gate.2) |
| `api_routes` | `ops.resync`, `accounts.purge` **ported**; `overlap.catalyst_scan` gated (R8.ar4). API: `GET /overlap`, `/overlap/reviews` ×2, explore `/watchlist` ×2 ported; R7.15 closed; 7 screener routes stay on Python (R8.ar6) |
| `curated` | `cb.compute_metrics`, `cb.sip_reminders`, `cb.derive_dividends`, `cb.sync_batches`, `cb.rebalance_notify` |
| `backtest_engine` | `backtest.run` |
| `sleeve_backtests` | `swing.backtest`, `vbt.backtest`, `options.backtest` |
| `swing_eod` | `swing.detect`, `swing.eod`, `swing.weekend`, `swing.catalyst`, `swing.check_detect_fresh`  **ported (RS6 lane, R-se.1)**: all 5 in `PORTED`; task_diff EQUAL py,rs on 7 cases run (check_detect_fresh ok/stale, detect published/unpublished/bad-date/no-user, eod no-market-row); the other 20 cases in `swing_eod.json` not yet run (Maulik's code-first order); 14 DB tests `--ignored` (`BASKFY_RS_SWING_EOD_DB_URL`) |
| `swing_live` | `swing.premarket`, `swing.scan_now`, `swing.scan_after_login`, `swing.scan_sweep`, `swing.timing_probe`, `swing.check_monitor_started`, `swing.check_orders_after_cutoff`, `swing.check_gtt_at_1515` — **all 8 ported**; `task_cases/swing_live.json` **43 of 43 EQUAL py,rs** (R8.x lane label R-sl.1). Added for reuse: `swing_live::published_session` (C3's one rule), `swing_live::producer` (`apply_async`), the detector's `ScanExtras`, core `live_gap`/`live_gap_score`, five `SWING_*` alert names; `swing_eod`'s plan pieces widened to `pub(crate)` (no body changed) |
| `twt_jobs` | `twt.detect`, `twt.evening`, `twt.morning`, `twt.scan`, `twt.scan_publish` |
| `vbt_jobs` | `vbt.detect`, `vbt.evening`, `vbt.morning`, `vbt.rescan`, `vbt.rescan_sweep`, `vbt.check_detect_fresh`, `vbt.check_orders_past_expiry`, `vbt.check_naked_positions`, `vbt.check_positions_without_bars` |
| `portfolio` | `portfolio.eod_nav`, `pipeline.capture_kite_trades` |
| `ops_alerts` | `ops.reap_abandoned_runs`, `ops.check_publish_deadline`, `ops.check_kite_token`, `ops.check_queue_backlog`, `alerts.sweep_webhooks`, `kite.login_nudge` (+ `refresh_kite_session_quietly`) |
| `fno` | `fno.ingest_bhavcopy`, `fno.index_daily`, `fno.scan`, `fno.weekly`, `fno.alerts`, `fno.retest`, `fno.spread_sample` |
| `options` | `options.refresh_master`, `collect_chain`, `index_bars`, `index_bars_eod`, `scan`, `plan_o1`, `plan_o2`, `plan_o3`, `weekly`, `checks`, `backfill_index_bars` |
| `backfills` | `pipeline.bhavcopy_ingest`, `eq_bars.session` |
| `clis` | `desk.daily`, `desk.autorun`, `desk.daily_check`, and the twelve `*_cli.py` modules as subcommands |

- **swing_live (30 Oct, R-sl.1):** all 8 tasks ported. The first `task_diff` batch was 17 of 25 EQUAL; every DIFFER was an unmasked seed stamp or the login `run_id` quirk, both fixed in the cases. The full 43-case rerun was not done (CODE FIRST).

### Wave 6 foundation — harness results (30 Oct 2026; bundles `base` sha256 33841c9f… (unchanged), `session` sha256 994dd6e6…)

| Case file | Sides | Result | What it exercised |
|---|---|---|---|
| `pipeline.json` | **py,rs** | **12 of 12 EQUAL** | `pipeline.nightly` on Beat's path at 13:15 and 17:59:59 on an unpublished session (skipped until 18:00 IST), 19:00 on a published day, a Saturday; a bad date (`ValueError`), an unknown keyword (`TypeError`); `session_catch_up` before the cutoff and on a Saturday morning after a seeded publish; `reprocess_instrument` — KPIGREEN 633 bars over 4 actions, CUPID positional 701 bars, an instrument with no action 698 bars (`ohlcv_daily` 3,887,194 rows EQUAL each time), a missing argument |
| `pipeline.json` | py,py | 12 of 12 EQUAL | the same, the harness against itself |
| `fno.json` | py,py | 3 of 3 EQUAL | the real 28 Oct F&O zip parsed from the archive with no request (35,033 rows in the file, 15,125 kept), the 29 Oct ban list; 29 Oct with no file: archive miss, two 404s from the upstream, `MISSING` at 23:30, `FNO_BHAVCOPY_MISSING` mailed; the disabled refusal |
| `options.json` | py,py | 5 of 5 EQUAL | through the production `kiteconnect` and a test token: `/instruments/NFO` (27,495 option contracts), three `/instruments/historical` minute pulls at 13:15:20 (76 recorded bars per index), one `/quote` of the recorded chain (105 rows); no session; collector disabled |

The four wired tasks are the only ones `py,rs` can be EQUAL on; every other case file is the lanes' to
fill (`fno.json` and `options.json` carry the foundation's proofs of the replay upstream, which a
Rust side answers `NotPorted` today). Gates: `cargo fmt --check` clean, `cargo clippy --workspace
--all-targets -D warnings` clean, `cargo test --workspace` on the bed **72 targets, 1,200 passed,
0 failed, 45 ignored** (wave 5's 1,184 plus 16 new; the same 45 ignored), 1,702 of 1,702 goldens.
Every database this work created was dropped; Redis 6 and 7 flushed; nothing listens on 18898/18899.

**What the py,py runs caught in the harness** (R8.2): a process-wide `datetime` patch broke polars
on both Python sides alike (fixed: production modules only); microsecond clock drift changed the
options collector's greeks (fixed: the clock advances in whole seconds on both sides). Masks added,
each printed: `fo_underlying_daily.updated_at`, `fo_ingest_day.updated_at` (Postgres `now()`), an
alert's `raised_at` and a report's `seconds` in two payloads.

**Open before a task cuts over (R8.1):** the Celery shim per task; the swing/VBT/TWT detect tasks
need `steps::sleeves`' detectors with `pipeline_run_id: Option<i64>` (their lanes widen the
signature, never fork the detector); the API's two copies of the Celery producer collapse into one
public module the worker can use (the sweeps enqueue); `refresh_kite_session_quietly`; Kite
historical minute candles in the `session` bundle (options index bars).

## Wave 5 integration (RS5, 30 Oct 2026) — the nightly in Rust, all 14 steps, every chain scenario EQUAL

The foundation (RS5.0) and five lanes (ingest, index, compute, gate, sleeves), each adversarially
reviewed, merged and graded together on the bed (2026-10-29 replay day, bundle `base` sha256
33841c9f…). The integrator ported the one step no lane owned, `refresh_basket` (R7.14), graded the
catch-up decision (R7.15) and renumbered the lanes' decisions R7.9–R7.13.

| Step | Rust | Graded (py,rs) |
|---|---|---|
| 1 `refresh_instruments`, 2 `fetch_daily_bars`, 3 `fetch_corporate_actions`, 4 `apply_adjustments` | `steps/ingest/` (R7.11) | ported; lane step runs fresh/rerun (+ gate-refuse for bars) EQUAL |
| 5 `refresh_index_membership`, 6 `refresh_index_snapshots` (+ fundamentals) | `steps/index/` (R7.9) | ported; fresh/rerun/provider-failure-midway, `index-edges`, `fundamentals-refused` EQUAL |
| 7 `compute_factors` (+ desk score), 8 `compute_market_health` | `steps/compute/` (R7.12) | ported; fresh/rerun/gate-refuse + three `--mutate` edge sets EQUAL |
| 9 `data_quality_gate`, 10 `publish` | `steps/gate/` (R7.10) | ported (the `ranking_terms` warm path raises `NotPorted`, R-gate.2; none on the bed) |
| 11 `refresh_basket` | `steps/sleeves/basket.rs` (R7.14) | **ported by the integrator**; `--step` fresh EQUAL with `FULLY_INVESTED` false and true (15 names, 43 suspect symbols) |
| 12 `compute_swing`, 13 `compute_vbt`, 14 `compute_twt` | `steps/sleeves/` (R7.13) | ported; fresh/rerun + `--mutate` edges/nobars/thin EQUAL |
| `catch_up.py` decision | `catch_up.rs`, `catch-up --now` | 11 of 11 moments EQUAL (`catch_up_diff.py`) |

**`nightly_diff.py --chain`, Python vs Rust — 7 of 7 scenarios EQUAL** (payload, `pipeline_run`,
`pipeline_run_step`, every touched table, 99 sequences, Redis, mail; masks only R7.7's):

| Scenario | Outcome (both sides) | Tables compared besides the run tables |
|---|---|---|
| `fresh` | succeeded, `data_version` 44, 14 steps | 18 (`ohlcv_daily` 3,887,193, `factor_daily` 928,614, `index_member_daily` 1,081,620, `desk_score_daily` 218,553, `basket_snapshot` 1, …); Redis 11 keys |
| `rerun` (house rule 7) | succeeded, `data_version` 45 | 18; Redis 11 keys |
| `gate-refuse` | failed at `data_quality_gate`, chain rows committed | 10; 1 alert mail |
| `non-trading-day` | aborted (2026-10-31, weekend) | 0 |
| `provider-failure-midway` | failed at step 6, chain rolled back | 5; 1 alert mail |
| `index-edges` | failed at `data_quality_gate` (the halved/raised snapshots) | 11; 1 alert mail |
| `fundamentals-refused` | succeeded, `data_version` 44 | 17; Redis 11 keys |

**Catch-up** (`tools/parity/catch_up_diff.py`, clock injected, never the system's): on the bed,
2026-10-30 at 09:15, 13:15 and 17:59:59 IST proposes nothing; at 18:00:00 and 21:30 it proposes
2026-10-30; on Saturday 31 Oct and Monday 2 Nov pre-open it proposes 2026-10-30. With 29 Oct's run
deleted: 13:15 and 17:59:59 on the 29th propose nothing, 18:00:00 proposes the 29th. 11 of 11 EQUAL.
A day still in session is never proposed.

**Gates.** `cargo fmt --check` clean; `cargo clippy --workspace --all-targets -D warnings` clean;
`cargo test --workspace` on the bed — 71 targets, **1,184 passed, 0 failed, 45 ignored** (the 14
historical, 30 lane scratch-DB tests and 1 score-diff fixture test; the 30 pass with
`BASKFY_RS_SCRATCH_DATABASE_URL` and `-- --ignored db_tests`), 1,702 of 1,702 goldens.

**API re-graded.** GET, all six case files: **2,285 of 2,285 EQUAL** (`BASKFY_SOLE_USER_ID=1`).
Writes: foundation, accounts, portfolio, sleeves, sleeves_nobroker, sleeves_nobroker_ratelimit
EQUAL with every side effect EQUAL; **screens 259 of 260**, side effects EQUAL — the one difference,
`probe-exit-huge-int`, is **pre-existing (RS4c) and clock-dependent**: graded at 12:36 IST the market
is open, so `POST /cb/plans/exit` builds a plan instead of the closed-market body, and Rust's `i128`
leg quantity saturates on Python's 42-digit integer (and the case masks neither `desk_plan_id`, a
`uuid4`, nor `expires_at_hint`). `baskfy-api` is unchanged by this wave. Owner: the API (R7.15).

**Audit.** No order, GTT or Kite write path in `baskfy-worker` (`law_two.rs`, and a grep); the only
network is the providers' guarded client (the replay arms the block); no secret logged; `law_one`
green; Python's worker and desk code unchanged (`git diff decile-blueprint kite-momentum-rebalancer`
empty); `baskfy` unchanged (`ohlcv_daily` 3,887,194, `pipeline_run` 96, `pipeline_run_step` 679,
`data_version` 44, `basket_snapshot` 0, before and after); the bed holds only `baskfy`; Redis
indices 3, 4, 11, 12 empty; nothing of this run listens on 18xxx. Other agents' harnesses
(`desk_diff.py`) shared the bed and the lock during the run; none of their databases was touched.

**Open before the nightly cuts over:** R7.1's Celery shell-out; R7.4 (spans, metrics), R7.5
(Sentry, webhook); R-gate.2's `ranking_terms` warm path; `BASKFY_DESK_ROOT` on the deployed binary
(R7.14); the `session_catch_up` body (nightly per day, swing EOD, holiday demotion; R-gate.4);
the residuals listed in R7.9–R7.13.

**Next:** the worker's remaining Celery tasks — screen runs together with the 11 API write routes
waiting on the screener (R6.13), backtests, the sleeves' evening and premarket jobs, holdings sync
(read-only), alerts — then the desk.

## Wave 5 foundation (RS5.0, 30 Oct 2026) — `baskfy-worker`, the replay provider, `nightly_diff.py`

**Not committed** (the integrator commits). Decisions R4.14 (closed), R7.1–R7.7.

| Piece | Python | State |
|---|---|---|
| S3 retries (R4.14) | boto3 legacy mode | **closed**: 5 attempts, botocore's retryable statuses/codes/socket errors, `random() * 2**(n-1)` backoff; `tests/s3_retry.rs` 8 cases on a loopback stub |
| crate `baskfy-worker` (lib + bin) | `services/worker` | declared in the workspace; no new third-party dependency (R7.2) |
| `steps.rs` | `steps.py` | **ported**: the 14 step strings in order, `NIGHTLY_CHAIN`, `POST_PUBLISH_STEPS`, statuses, `StepOutcome` (`dict.update` order), `record_step` (two upserts per step, JSON `null` for no detail, the failure row never replacing the failure), `open_run`/`adopt_run`/`close_run` |
| `orchestrator.rs` | `orchestrator.py` | **ported**: `try_lock_pipeline` (Python's key `0x0BA5C1FA`), `run_nightly_pipeline` (aborted day, held lock, `HardFailure` caught and committed), the chain as one block per step (R7.6), the desk-score savepoint, the three post-publish wrappers that cannot raise |
| `nightly.rs` | `celery_tasks.nightly_pipeline` (explicit date) | **ported**: three transactions, `fail_run`, the alerts, the return value |
| `ops.rs`, `calendar.rs` | `ops.begin_run/fail_run/is_trading_day`, `calendar.classify/require_trading_day` | ported |
| `alerts.rs` | `alerts.Alert/dispatch`, `ops.alert_for_outcome` | log + email ported; Sentry and webhook not (R7.5) |
| `settings.rs` | `WorkerSettings` | ported, 53 fields held to a Python-generated fixture |
| `catch_up.rs` | `catch_up.py` | constants (`SESSION_DATA_READY_IST = 18:00`) and `_has_had_time_to_publish` ported; the sweep is the **gate lane's** stub |
| CLI | `pipeline_cli`, the Celery task | `nightly --date`, `step <name> --date --run-id [--context]`, `catch-up`, `--replay-bundle` |
| step bodies | `tasks/*.py` | **declared stubs** (`NotPorted`) in `steps/{ingest,index,compute,gate,sleeves}.rs`, one file per lane |
| replay provider | — | Rust `baskfy_providers::replay` + Python `tools/parity/replay_provider.py`; `replay_selfcheck.py` **22 of 22 calls EQUAL** on `base` and `provider-failure-midway` (R7.3) |
| bundle builder | — | `tools/parity/replay_build.py --date 2026-10-29` (deterministic; three variants) |
| harness | — | `tools/parity/nightly_diff.py` (`--chain`, `--step`, five scenarios, lock, budget, cleanup; R7.7) |

**The harness proved (Python vs Python, 2026-10-29 replay day).** See "Wave 5 — harness results" below
for every run's line; all tables, sequences, Redis and mail EQUAL with only the masks R7.7 lists.

**The Rust skeleton runs end to end.** Python vs Rust on a non-trading day (2026-10-31) is
**EQUAL** — payload, run rows, sequences, Redis, mail — because that path is all foundation code.
On a trading day the Rust chain stops at step 1 with `NotPorted` (rolled back, run failed on its
own transaction, `pipeline_failed` mailed), and `--probe-steps` shows every one of the 14 steps
answering `NotPorted` through `baskfy-worker step`.

### Wave 5 — harness results (RS5.0, 30 Oct 2026; 2026-10-29 replay day, bundle `base` sha256 33841c9f…)

| Run | Sides | Result |
|---|---|---|
| `--chain --scenario fresh` | py,py | **EQUAL**: payload (succeeded, `data_version` 44), `pipeline_run` 96, `pipeline_run_step` 679, 18 touched tables (`ohlcv_daily` 3,887,193 rows, `factor_daily` 928,614, `index_member_daily` 1,081,620, `desk_score_daily` 218,553, …), 99 sequences, Redis 11 keys, mail 0 |
| `--chain --scenario rerun` | py,py | **EQUAL** (idempotent re-run, house rule 7): `data_version` 45, the same tables, Redis 11 keys |
| `--chain --scenario gate-refuse` | py,py | **EQUAL**: failed at `data_quality_gate` ("1687 bars against a 10-day median of 4216 (threshold 3795)"), run closed failed, chain rows committed, 1 alert mail |
| `--chain --scenario provider-failure-midway` | py,py | **EQUAL**: `CapabilityNotAvailable` at step 6, chain rolled back, run failed on its own transaction, 1 alert mail |
| `--chain --scenario non-trading-day` | **py,rs** | **EQUAL**: 2026-10-31 aborted ("is not an NSE trading day: weekend"), run rows and sequences equal — the foundation's own path |
| `--step compute_market_health --scenario fresh` | py,py | **EQUAL** (prime of 7 steps cached as `rsn_primed_computemarkethealth_…`, reused by the next run) |
| `--step compute_market_health --scenario fresh` | py,rs | DIFFERENT as expected: Rust raises `NotPorted`, rolled back |
| `--chain --scenario fresh` | py,rs | DIFFERENT as expected: Rust stops at step 1 (`NotPorted`), run failed, `pipeline_failed` mailed |
| `--probe-steps` | rs | every one of the 14 steps answers `NotPorted` (steps 12–14 record `skipped` with `skipped_reason: "NotPorted: …"` and exit 0, because the wrappers cannot raise) |

Masks the Python-vs-Python runs needed, and nothing else (`TABLE_MASKS`, R7.7): `instrument.created_at/updated_at`,
`basket_snapshot.computed_at/computed_ms`, `created_at` on `screen_run`, `sw_market_daily`, `sw_setup_daily`,
`tw_breadth_daily`, `tw_signal_daily`, `tw_state_daily`, `vb_breadth_daily`, `vb_signal_daily`, and
`refresh_basket`'s `computed_ms` detail. **No nondeterminism found in Python** beyond wall clocks and
measured durations: every row, sequence, Redis value and mail body agreed across two runs (no
`PYTHONHASHSEED` pinning). A Python chain over the replay takes ~3 minutes; a side pair with the
digests ~8–9 minutes; a fresh 3 GB template ~1 minute.

Also green at RS5.0: `replay_selfcheck.py` 22/22 on two bundles; `cargo fmt --check`, `clippy --workspace
--all-targets -D warnings`, `cargo test --workspace` on the bed (71 targets, 1,038 passed, 14 ignored,
1,702 goldens); `api_diff.py` GET 2,285 of 2,285 EQUAL (`BASKFY_SOLE_USER_ID=1`); writes spot check
`writes_foundation.json` 74 of 74, side effects EQUAL. The bed holds only `baskfy` afterwards (caches
dropped with `--drop-cache`), Redis indices 11 and 12 empty.

### Wave 5 plan — the lanes (the foundation's contracts are fixed; R7.2, R7.6)

| Lane | Owns | Python it ports | Grade with |
|---|---|---|---|
| **ingest** | `steps/ingest.rs` | `tasks/instruments`, `listings`, `bars` (+ `bhavcopy_backfill` as the bars need it), `corporate_actions`, `adjustments` (house rule 5: the nightly's **unbounded** `apply_adjustments` is deliberate), `calendar.reconcile_calendar` | `--step refresh_instruments … apply_adjustments`, scenarios `fresh` and `rerun` |
| **index** | `steps/index.rs` | `tasks/membership`, `snapshots`, `fundamentals` | `--step refresh_index_membership`, `refresh_index_snapshots`; `provider-failure-midway` |
| **compute** | `steps/compute.rs` | `tasks/factors` (the polars engine), `desk_score`, `market_health` — pulls `baskfy_core` modules with no goldens yet (goldens first) | `--step compute_factors`, `compute_market_health` |
| **gate** | `steps/gate.rs`, `catch_up.rs` | `tasks/quality`, `publish` (cache purge + warm: needs the screener port shared with R6.13's routes), `catch_up` + `session_catch_up` | `--step data_quality_gate`, `publish`; `gate-refuse` |
| **sleeves** | `steps/sleeves.rs` | `tasks/basket`, `swing.run_detect_swing`, `vbt.run_detect_vbt`, `twt.run_detect_twt` (law 2: detection places nothing) | `--step refresh_basket … compute_twt` |

Then the integrator: `--chain` for all five scenarios, `py,rs`, EQUAL; the Celery shell-out (R7.1)
is the cutover. Open before cutover: R7.4 (spans/metrics), R7.5 (webhook, Sentry), the
`WorkerSettings` parser collapse (R7.2), Kite session refresh stays in the Celery task.

## Wave 5 lane nightly-sleeves (RS5, 30 Oct 2026) — steps 12–14 ported, 6 of 6 runs EQUAL

**Not committed** (the integrator commits). Decisions: R7.13.

| Python | Rust | State |
|---|---|---|
| `tasks/swing.run_detect_swing` (nightly path) | `steps/sleeves/swing.rs` | **ported**; `--step compute_swing` EQUAL in `fresh` and `rerun` |
| `tasks/vbt.run_detect_vbt` (nightly path) | `steps/sleeves/vbt.rs` | **ported**; `--step compute_vbt` EQUAL in `fresh` and `rerun` |
| `tasks/twt.run_detect_twt` + `run_twt_manage_qulla` | `steps/sleeves/twt.rs` | **ported**; `--step compute_twt` EQUAL in `fresh` and `rerun`; `run_twt_ratchet` not ported (unreachable, R7.13) |
| `baskfy_core.exits.qulla` | `steps/sleeves/qulla.rs` (private) | ported; lift into the core later |
| `baskfy_core.precision` (sleeves' columns) | `steps/sleeves/precision.rs` (private copy) | ported; collapse with `factors::precision` later |
| `swing.market.breadth_snapshot/ema/index_reading/market_gate/drawdown_pct`, `swing.indicators.liquid_expr` | `steps/sleeves/swing_market.rs` (private) | ported; move to `baskfy_core::swing::market` later |
| `tasks/basket.run_refresh_basket` (step 11) | `steps/sleeves/basket.rs` | **ported by the RS5 integrator** (R7.14); `--step refresh_basket` EQUAL and every `--chain` EQUAL |

Adversarial review (30 Oct): `nightly_diff.py --mutate` scenarios **edges**, **nobars**, **thin** over
`fresh` for all three steps, plus edges over `rerun` for `compute_swing`: 10 of 10 EQUAL (R7.13,
"Adversarial review"). One Python edge listed, not ported: VBT `signal_rows`' 100-row schema inference.

Unit tests: 30 in the lane's modules (precision, conversions, ETF patterns, `with_storage_columns`,
qulla `decide` on `test_twt_qulla.py`'s three scenarios, swing helpers, the gate, breadth, EMA, drawdown).
`cargo clippy -p baskfy-worker --all-targets -D warnings` and `cargo test -p baskfy-worker` green.

## Wave 5 lane nightly-ingest (RS5, 30 Oct 2026) — steps 1–4 ported, 8 of 8 step runs EQUAL (+ gate-refuse)

**Not committed** (the integrator commits). Decision R7.11.

| Piece | Python | State |
|---|---|---|
| `steps/ingest.rs` | the step bodies of `_run_chain` blocks 1–4 | the foundation's signatures, filled; `reprocess_instrument(conn, id, as_of)` added (public) |
| `ingest/instruments.rs` | `tasks/instruments` (`run_refresh_instruments`, `_merge`, `_batched`, `active_instruments`), `tasks/listings` (`run_refresh_listings`, `store_listings`) | ported |
| `ingest/bars.rs` | `tasks/bars` (bhavcopy leads, Kite top-up, fallback, M84 top-up), `bhavcopy_backfill` (`backfill_bars_from_bhavcopy` on the chain's transaction, `_rows_for_day`, `upsert_day`) | ported |
| `ingest/calendar.rs` | `calendar.reconcile_calendar` + M62's publication check | ported |
| `ingest/corporate_actions.rs` | `tasks/corporate_actions` | ported |
| `ingest/adjustments.rs` | `tasks/adjustments` (`run_apply_adjustments`, `reprocess_instrument` with `as_of`, `_rescale_deep_segment`, the polars frame `_load_raw_bars` builds) | ported; the nightly passes no `as_of` (house rule 5) |
| `ingest/adjust.rs` | `baskfy_core.adjustments` (pure) | ported, polars 1.43 decimal semantics reproduced (R7.11) |
| `baskfy_core::decimal::WideDecimal` | — | **added** `mul_exact`, `rescale_exact`, `coefficient_len` |

**Graded** (`nightly_diff.py --step <name>`, sides `py,rs`, 2026-10-29 bundle `base`): `refresh_instruments`
fresh and rerun (`instrument` 11,524 rows, `instrument_id_seq` moved equally); `fetch_daily_bars` fresh and rerun
(`ohlcv_daily` 3,887,193 rows, `trading_day` 5,844); `fetch_corporate_actions` fresh and rerun (`corporate_action`
1,172 rows, its sequence); `apply_adjustments` fresh and rerun (`ohlcv_daily` after rebuilding the touched
instruments, deep segments included) — **8 of 8 EQUAL**, every step row (status, counts, detail JSON), sequences,
Redis (0 keys) and mail (0) included, with no mask beyond R7.7's. Also `fetch_daily_bars --scenario gate-refuse` (the thinned bundle, so the Kite pass and its per-symbol failures run): **EQUAL** (`ohlcv_daily` 3,884,676 rows).

**Tests.** 29 unit tests in the lane's modules: `test_adjustments.py`'s factor formulas, refusals to guess, series
continuity, cumulative products and idempotence; the polars rounding rules and a three-action history copied from a
Python run; the frame's 100-row inference window; `_unadjust`/`_price`/`_issue_price`; `test_instrument_upsert_batching.py`'s
four `_batched` assertions; the merge, the register's first-wins dedupe (PARIN), the corporate-action collapse
(SIYSIL), the bhavcopy row matcher. The step-level database assertions of `test_pipeline_steps.py` (calendar,
`record_step`) and `test_reference_data.py` (listings) are graded by the harness rather than re-run as Rust DB tests.

**Adversarial review (30 Oct 2026).** One defect fixed: the raw-bar frame turned a value that does not fit
`Decimal(38, s)` into a `ComputeError` where polars' `strict=False` gives null (R7.11, review correction). A
differential of 1,900 generated histories (Python's `adjust_bars` + `_price`/`_factor`/`int(volume)`/`_issue_price`/
deep-segment maths vs the Rust functions) agrees everywhere. Seven further harness runs, each **EQUAL**, with
`--mutate` edges applied to both clones: `apply_adjustments` fresh (every resolver branch incl. INVALID/INSUFFICIENT,
a new split rescaling a deep segment, deep rows at factor 0 and the zero-price fallback, raw-less rows across the
100-row inference window, an all-`kite_adjusted` instrument) and fresh plain; `refresh_instruments` fresh (a re-listed
symbol taking a new identity, register/Kite corrections); `fetch_daily_bars` fresh (unmatched bhavcopy series, names
without tokens), fresh with the day not a session (bhavcopy lead and top-up read no day; full Kite pass), rerun with
the day's rows already adjusted (AF 0.6); `fetch_corporate_actions` fresh (half the window re-inserted, half changed,
one symbol unknown).

**Open.** R7.11's residuals (`_issue_price` on a `NaN` payload; a database error's message inside the bhavcopy
write; polars' overflow message text and the scale-over-38 panic, both unreachable on real prices); `provider-failure-midway` refuses step 6 only, so it does not reach this lane's code differently from `fresh`.

## Wave 5 lane nightly-gate (RS5, 30 Oct 2026) — steps 9–10 and `catch_up.rs` ported, 5 of 5 step runs EQUAL

**Not committed.** Decisions: DECISIONS-RS **R7.10** (lane labels R-gate.1–5), all ⚠ UNREVIEWED.

| Python | Rust | State |
|---|---|---|
| `tasks/quality.py` (8 assertions, `GateReport`, `to_payload`) | `steps/gate.rs`, `steps/gate/quality.rs` | **ported** — the same SQL per check; `Decimal` moves; SQLAlchemy's `Numeric(14,2)` typing of `percentile_cont` reproduced (found by the harness) |
| `statistics.median`/`fmean`/`pstdev`, `f"{x:.Nf}"`, `.0%` | `steps/gate/pystats.rs` | **ported bit for bit** (private big integer; 60 CPython lists + the bed's 59 medians) |
| `tasks/publish.py` + `baskfy_api.screener` purge/warm (legacy SQL path, desk-score attach, provenance, `record_run`) | `steps/gate/publish.rs` | **ported**; a `ranking_terms` screen raises `NotPorted` (R-gate.2, cutover gate with R6.13) |
| `catch_up.py` (`landed`, `gate_refused`, `unlanded`, demotion, `mark_inferred_holiday`; 18:00 IST) | `catch_up.rs` | **ported**; the Celery `session_catch_up` body (nightly per day + `swing_eod_task`) is not (R-gate.4) |
| `tasks/basket.py` (`refresh_basket`, step 11) | `steps/sleeves/basket.rs` | **ported by the RS5 integrator** (R7.14; R-gate.5 closed) |

**Graded** (`tools/parity/nightly_diff.py`, py vs rs, 2026-10-29 replay day):

| Run | Result |
|---|---|
| `--step data_quality_gate --scenario fresh` | **EQUAL** (step row incl. every `observed`, run rows, sequences) |
| `--step data_quality_gate --scenario gate-refuse` | **EQUAL** — the partial universe is refused on both sides: `[1] 1687 bars against a 10-day median of 4216 (threshold 3795)`, no `data_version` |
| `--step data_quality_gate --scenario rerun` | **EQUAL** (house rule 7: the re-judged day writes the same row) |
| `--step publish --scenario fresh` | **EQUAL** — `data_version` 44, `screen_run` 280 rows, 11 Redis keys, sequences |
| `--step publish --scenario rerun` | **EQUAL** — `data_version` 45 (bumped once), the same 11 keys and rows |

**Tests:** `cargo test -p baskfy-worker` — the lane's 40 unit tests (verdicts of every
`test_quality_gate.py` / `test_quality_universe_sources.py` assertion, the catch-up filter, the
statistics, publish helpers) plus 16 database tests (`steps/gate/db_tests.rs`, `TEMP` tables on
`BASKFY_RS_SCRATCH_DATABASE_URL`, `ignored` without it; 16 of 16 pass on the bed container's
`postgres` database). Clippy `-D warnings` clean for the crate; the lane's files are `rustfmt`-clean.

**Open:** the `ranking_terms` warm path (R-gate.2), the
`session_catch_up` task body (R-gate.4), Redis error texts in `cache_*_failed` (R-gate.2).

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

## Wave 4c integration (RS4c, 30 Oct 2026) — 82 of Python's 94 write routes ported, 1,175 of 1,175 writes cases EQUAL

The foundation (RS4c.0) and four lanes (writes-accounts, writes-portfolio, writes-screens,
writes-sleeves), each with an adversarial verifier, merged and graded together on the bed.

**Gates, all green.** `cargo fmt --check`; `cargo clippy --workspace --all-targets -D warnings`;
`cargo test --workspace` on the bed — 64 test targets, **1,013 passed, 0 failed, 14 ignored** (the
same 14), 1,702 of 1,702 graded goldens.

**GET.** `api_diff.py` over the six GET case files: **2,285 of 2,285 EQUAL** with
`BASKFY_SOLE_USER_ID=1` and **2,285 of 2,285** without it.

**Writes** (`api_diff.py --writes --lane rsint`, one lane template, three fresh clones per run;
every response and every side effect — the scenario's tables row by row, every sequence, both
Redis databases, outbound mail):

| Scenario | Responses EQUAL | Side effects | Also run with `BASKFY_SOLE_USER_ID=1` |
|---|---:|---|---|
| `writes_foundation.json` | 74 / 74 | EQUAL | 74 / 74, EQUAL |
| `writes_accounts.json` | 336 / 336 | EQUAL | 336 / 336, EQUAL |
| `writes_portfolio.json` | 270 / 270 | EQUAL | 270 / 270, EQUAL |
| `writes_screens.json` | 260 / 260 | EQUAL | (the file sets it) |
| `writes_sleeves.json` | 217 / 217 | EQUAL | (the file sets it) |
| `writes_sleeves_nobroker.json` | 9 / 9 | EQUAL | (the file sets it) |
| `writes_sleeves_nobroker_ratelimit.json` (new, R6.40) | 9 / 9 | EQUAL | (the file sets it) |
| **total** | **1,175 / 1,175** (1,166 before R6.40's file) | EQUAL | |

**Write routes: 94 in Python** (decorators under `routers/`; `public` has none).

| Lane | Python | Ported | Not ported, and why |
|---|---:|---:|---|
| foundation (`auth`) | 6 | 5 + `/auth/google` partly | Google's RS256 verifier and `link_google_identity` (R6.3, cutover gate) |
| writes-accounts (`admin`, `alerts`, `api_keys`, `billing`, `support`, `webhook_endpoints`) | 20 | 20 | — |
| writes-portfolio (`portfolios`, `portfolio_overview`, `trades`, `brokers`, `track_b`) | 16 | 15 | `POST /portfolios/{id}/rebalance` runs the screener (R6.23) |
| writes-screens (`screens`, `watchlist`, `managers`, the `curated_*` routers) | 30 | 24 | `/screens/preview`, `/explain`, `/selection`, `/{id}/run`, `/cb/baskets/from-screen`, `/cb/baskets/{slug}/recut` run the screener (R6.13) |
| writes-sleeves (`swing`, `twt`, `vbt`, `sleeves`, `options`, `fno`, `overlap`, `backtests`) | 20 | 18 | `PUT`/`DELETE /overlap/reviews` re-read through `GET /overlap`, itself unported (R5.21, R6.29) |
| unassigned (`explore`) | 2 | 0 | `POST /watchlist`, `DELETE /watchlist/{slug}` were in no lane's brief (R6.40) |
| **total** | **94** | **82 + 1 partly** | **11 on Python** |

Ported, but with a gate before cutover: `POST /backtests` only with `BASKFY_BACKTEST_EXECUTOR=celery`
(R6.28); `sync-holdings` and `sync-today` answer 503 where Python would read Kite live (R6.22);
`/brokers/{id}/connect` must cut over with `/brokers/callback` or share the state file (R5.27);
mailing routes on a STARTTLS/AUTH or Resend deployment (R6.6).

**Fixed at integration (R6.40).** With no usable Redis URL, Python builds no rate limiter and
serves unmetered; Rust refused every request with 503 (found by the writes-sleeves verifier,
R6.32). Graded by the new `writes_sleeves_nobroker_ratelimit.json`.

**Audit.**
* **No order path.** No route places, modifies or cancels an order or GTT, and the API crate makes
  no Kite call (`routes/kite.rs` serves two GETs that render a basket file). The only
  `place_order`/`place_gtt` strings are the lanes' source tests asserting their absence
  (`no_write_here_reaches_a_broker`, `no_module_of_this_lane_can_reach_an_order`, …).
* **GET handlers cannot reach the write pool** (R6.2): `tests/write_access.rs` (every handler that
  takes `WritePool` is mounted on a write verb only; nothing else touches the pool) and the unit
  test that a GET request gets no pool — both green.
* **No secret in any log.** Every harness server log (GET and writes runs) was scanned for the bed
  JWT secret, the scenario's test Razorpay/webhook secrets and the bed password: none found.
* **The public API is shut** (D9): `routes::public` is never merged; the GET cases grade its 404s.
* **The bed.** Every harness-created database was dropped (the lanes' seven `rsw_tpl_*` templates
  and this run's); `baskfy-rs-pg` holds only `baskfy`. Redis indices used are empty. `baskfy` itself
  was not written: its `pg_stat_user_tables` insert/update/delete counters were identical before
  and after every writes run, and the main tables' row counts unchanged. (Compared against
  itself, not the live database — the bed rules forbid touching the live stack.)
* **Python.** The R5.28 suites (`test_api_trades`, `test_rank_history_visibility`,
  `test_api_instruments`, `test_broker_trades`, `test_instrument_appearances`, `test_api_screens`):
  **65 passed** on a scratch database in the bed container, dropped afterwards.

**Open, carried to the cutover list.**
* **Body before token** (R6.17, R6.30): a refused token with a malformed JSON body is Python's 400
  `json_invalid` and Rust's 401, and Python charges no rate limit for it. Foundation code
  (`versioned_dependencies`); no case grades it. Must be fixed before any write route cuts over.
* **Four pydantic emulations** (R6.12 `screens/body_model.rs`, R6.20 `portfolio/model.rs`, R6.25
  `sleeves/pyd.rs`, R6.33 `api_keys/model.rs`) and **two Celery producers** (R6.26, R6.36) to
  collapse — every case must stay EQUAL through it.
* The screener path (11 routes above) is one port: `execute_screen`, `rank_definition`, the screen
  cache, provenance and `record_run`.

## Wave 4c foundation (RS4c.0, 30 Oct 2026) — the write pool, the writes harness, the session routes

**Committed at RS4c** (the integration commit). Everything below is graded on the bed.

| Piece | Python | State |
|---|---|---|
| R5.28's two tenant-isolation defects | `routers/trades.py`, `routers/instruments.py` | **fixed in Python first, then Rust** (R6.1); Python spec tests added and failing on the old code; the affected Python suites green on a scratch database |
| shared helpers (sole tenant, pydantic lax `int`/`bool`, Python `int()`, IST clock, `latest_published_date`) | the lanes' five copies | **one `routes/common.rs`** (R6.9); the lanes' names delegate |
| JSON integers of any size (`jsonb`, bodies) | `json.loads` | **exact** (`OrderedJson::BigInt`, R6.10) |
| write-only Python paths | Starlette 405 | **405 in every lane** (37 registered empty, 4 real, 5 left to their GET route; R6.11) |
| read-write pool | `db.create_engine` / `get_session` | **`baskfy_db::WriteDb`**, handed only to POST/PUT/PATCH/DELETE (`session::write_access`); one transaction per request, commit on `Ok`, rollback on `Err` (R6.2); a unit test proves a GET handler cannot obtain it, a source test that no other file names it |
| request bodies | FastAPI + pydantic | `body.rs` (content-type rule, CPython's scanner positions, model errors), `email_address.rs` (`EmailStr`, ASCII exact over 4,064 Python vectors; R6.7) |
| mail | `email.sender` | console + plain SMTP ported; STARTTLS/AUTH and Resend **not** (R6.6) |
| `Idempotency-Key` | `idempotency.py` | ported (`idempotency.rs`, R6.8); no ported route uses it yet |
| `POST /auth/refresh`, `POST /auth/logout`, `PATCH /me`, `DELETE /me`, `POST /me/restore` | `routers/auth.py`, `auth_service.py` | **ported** (R6.3) |
| `POST /auth/google` | `routers/auth.py`, `auth_google.py` | refusals ported; **Google's RS256 verifier and `link_google_identity` not** (R6.3, a cutover gate) |

**Graded.**
* **GET** — `api_diff.py` over the six GET case files, **2,285 of 2,285 EQUAL** with
  `BASKFY_SOLE_USER_ID=1` and **2,285 of 2,285** without it (2,113 at RS4b, plus 4 rank-history
  cases and 168 write-only-path cases).
* **Writes** — `api_diff.py --writes --lane wc --smtp-port 18313 --cases
  tools/parity/api_cases/writes_foundation.json`: **74 of 74 responses EQUAL, side effects EQUAL**
  — tables `app_user`, `refresh_token`, `account_deletion`, `auth_identity`, `consent_record`
  (row changes against the run's base clone, masks printed), every sequence, both sides' Redis,
  and the deletion mail. The scenario covers every body-validation shape (no body, `null`, bad
  JSON at several positions, no/ wrong `Content-Type`, extras, lengths, `EmailStr` failures),
  CSRF, logout, refresh rotation with a captured token, reuse, expiry, the deletion with its mail,
  and the restore with the code captured from that mail.
* **Python** — the R5.28 specs plus the affected API suites (135 tests) on a scratch database.

**Found (for Maulik):** Python rolls back the refresh-token reuse revocation (R6.4) — reproduced.

**Cutover gates added:** `/auth/google` (verifier), any mailing route on a STARTTLS/AUTH or Resend
deployment (R6.6). Still open from RS4b: the live Kite overlay, `/baskets` without a snapshot,
`/brokers/callback`, `/backtests/{id}/events` in flight, `GET /overlap`.

## Wave 6 lane backtest_engine (RS6, 30 Oct 2026) — `baskfy.backtest.run` ported

* `tasks/backtest_engine/{job,loader,payload,risk_free}.rs`; `task_diff.py` backtest_engine.json 12/12 EQUAL py,rs (row incl. metrics_hash, Redis progress, three CSVs). DECISIONS-RS R-be.1–R-be.4.
* Not ported: the API's inline executor (R-be.1); `ranking_terms` screens raise `NotPorted` (R-gate.2).

## Wave 4c lane writes-screens (RS4c, 30 Oct 2026) — 24 of the lane's 30 write routes ported

**Committed at RS4c** (the integration commit). Routers: `screens`, `watchlist`, `managers`,
`curated_create`, `curated_engage`, `curated_plans`, `curated_customize`, `curated_investments`,
`curated_sip`, `curated_drift`, `curated_from_screen`.

| Route | State |
|---|---|
| `POST /screens` (Idempotency-Key), `PATCH`/`DELETE /screens/{public_id}`, `POST /screens/{public_id}/duplicate` (Idempotency-Key) | **ported** |
| `POST /screens/preview`, `/explain`, `/selection`, `POST /screens/{public_id}/run` | **not ported** — they run the screener (R6.13); Python serves them |
| `POST /watchlist/instruments`, `DELETE /watchlist/instruments/{symbol}`, `PUT /watchlist/discover-preferences` | **ported** |
| `POST /managers/me`, `PATCH /managers/{slug}` (+ `PATCH /managers/me`, Starlette's routing), `POST`/`DELETE /managers/me/baskets/{basket_slug}/publish` | **ported** (with `manager_onboarding` and `sebi_registration`, privately) |
| `POST /cb/baskets` | **ported** |
| `POST /cb/pending-actions/{action_id}/dismiss`, `/resolve` | **ported** |
| `POST /cb/plans/invest`, `/apply`, `/exit`, `POST /cb/investments/{id}/customize` | **ported** — previews, never orders (R6.16) |
| `POST /cb/investments/mark` (+ its `GET`, Starlette's routing), `PUT`/`DELETE /cb/investments/{id}/portfolio` | **ported** |
| `POST /cb/investments/{id}/sip` | **ported** (REMINDER only; AUTO refused) |
| `POST /cb/investments/{id}/drift/scan`, `/drift/fix` | **ported** (R6.14) |
| `POST /cb/baskets/from-screen`, `POST /cb/baskets/{slug}/recut` | **not ported** — they run the screener (R6.13) |

**Graded.** `api_diff.py --writes --lane ws --smtp-port 18227 --py-port 18221 --rs-port 18222
--redis-db-py 3 --redis-db-rs 4 --cases tools/parity/api_cases/writes_screens.json`: **221 of 221
responses EQUAL, side effects EQUAL** (15 tables row by row, every sequence, both Redis databases —
idempotency keys by presence, their values being each side's random public id — and mail: none).
The scenario covers every route's body shapes (no body, `null`, a list, a non-JSON content type,
extras forbidden and ignored, each field type's refusals), anonymous/expired/stale-epoch tokens,
the other tenant (`not-the-sole-tenant`, not-yours 404s), staff-only review, idempotent replays
(and a replay whose screen was deleted), 402 at the free plan's five screens, 409s, the 500s Python
gives (a duplicate drift symbol, a bigint overflow), and the GETs that show each effect. The GET
case files `foundation.json` + `accounts.json` stay **707 of 707 EQUAL** with the writes routed.

**Adversarial review (30 Oct 2026).** The 30 write decorators of the eleven routers were listed
again: 24 ported, 6 excluded with R6.13's reason. The reviewer added 39 cases (260 in all). They
cover a cookie-only caller, a per-user idempotency scope, staff against a private screen, a
refused definition, the duplicate of a deleted screen, the preferences INSERT branch (the seed
removes the owner's row), the subscriber against the investment routes, column overflows midway
through `POST /cb/baskets`, `/cb/investments/mark` and `/drift/fix`, a 31-digit amount, a zero
broker lot, and validator probes (`_`, Unicode digits, non-finite and huge numbers, duplicate keys,
charset and `+json` content types, dates from timestamps). Run on ports 18321/18322 with Redis
14/15: **260 of 260 responses EQUAL, side effects EQUAL**. Three port defects were found and
fixed (R6.18): rows were inserted one at a time where the ORM sends one multi-row INSERT
(sequence parity on a failure); plan quantities went through `quantize(0)`; dates from numbers
did not follow speedate's rules. One mismatch is still open, in the foundation: a refused token
with a malformed body (R6.17).

**Tests.** Unit tests per module (validator, manager state machine and SEBI formats, fee, weights
and slugs, SIP calendar, drift detection and rebase, plan arithmetic and market hours against
Python's answers, uuid4 shape) and a source test that no file of the lane names an order path.

## Wave 4c lane writes-portfolio (RS4c, 30 Oct 2026) — 15 of the lane's 16 write routes ported

**Committed at RS4c** (the integration commit). Routers: `portfolios`, `portfolio_overview`, `trades`,
`brokers`, `track_b` (`kite` has no write route).

| Route | State |
|---|---|
| `POST /portfolios` (Idempotency-Key), `PATCH`/`DELETE /portfolios/{id}`, `PUT /portfolios/{id}/holdings` | **ported** (symbol resolution's four tiers, `replace_holdings`, `check_move`) |
| `POST /portfolios/import-csv` (multipart, Idempotency-Key) | **ported** (python-multipart's parser, `csv.reader`, `portfolio_csv`; GET/PATCH/DELETE on the path reach `/{portfolio_id}` as in Starlette) |
| `POST /portfolios/{id}/rebalance` | **not ported** — it runs the screener (R6.23); Python serves it |
| `POST /portfolio`, `POST /portfolio/{id}/holdings`, `POST /portfolio/reconciliation/{item_id}/resolve` | **ported** (`_apply_allocation` statement for statement; bookkeeping, never an order) |
| `POST /trades/import` (multipart) | **ported** (`parse_tradebook_csv` with `csv.DictReader` and `strptime`, `store_fills`, `apply_trade_history`) |
| `POST /trades/sync-today` | **ported up to Kite's read** — every no-session answer; with a fresh session, 503 (R6.22) |
| `POST /brokers/{id}/connect` | **ported** — every refusal and the live branch (state minted, shared with Python's callback through `BASKFY_BROKER_OAUTH_STATE_PATH`) |
| `POST /brokers/{id}/sync-holdings` | **ported up to the live read** — unwired, DRY_RUN, no key, no session, an unusable session, fixture rows; with a fresh session, 503 (R6.22) |
| `POST /cb/paywall/checkout`, `/cb/public-signup`, `/cb/fees/collect` | **ported** (404 while their Track B flags stay false) |

**Graded.** `api_diff.py --writes --lane wportfolio --smtp-port 18213 --redis-db-py 6 --redis-db-rs 7
--py-port 18211 --rs-port 18212 --cases tools/parity/api_cases/writes_portfolio.json`: **188 of
188 responses EQUAL, side effects EQUAL** (`portfolio`, `portfolio_holding`, `broker_account`,
`broker_trade`, `reconciliation_item`, `symbol_alias` row by row, every sequence, both Redis
databases — the idempotency records included) — both without `BASKFY_SOLE_USER_ID` and with it.
The scenario covers every body shape (no body, `null`, a list, no content type, bad JSON before the
401, each field type's refusals, extras forbidden and ignored), the multipart reader's refusals (no
boundary, garbage, an unterminated body, a part with no name, a text field, url-encoded), the
tradebook's and the portfolio CSV's refusals (and the `csv.Error` 500 of a lone CR),
anonymous/expired/stale-epoch/garbage tokens, a cookie with no bearer, the other tenant on every
route, idempotent replays (and another user's key), the depth cap and the move refusals, the
allocation refusals, the reconciliation refusals, the 500s Python gives (one instrument reached by
two symbols, a smallint benchmark id), and the GETs that show each effect (`/portfolios`, the
roll-up, `/portfolio/{id}`, `/portfolio/holdings`, `/reconciliation`, `/activity`, `/overview`,
`/trades`). Four scratch runs graded the broker branches the default environment cannot reach
(a fixture file under `DRY_RUN`, `DRY_RUN=false` without a key, with a key and no session, with an
unreadable session and a shared state file): all EQUAL. The GET case files `portfolio.json` +
`foundation.json` stay **538 of 538 EQUAL** in both environments after the loaders moved onto one
connection (R6.19).

**Adversarial review (30 Oct 2026).** The reviewer appended 82 cases (Decimal and JSON edges —
booleans, integers beyond u64, `1e-1000`, `NaN` literals, duplicate keys; idempotency after a delete
and after a failed write; tenant probes against another user's portfolio on every route; ids beyond
`bigint`; allocation precision past 28 digits; Starlette's form limits and `charset`). Four form
cases differed — 1,001 multipart fields or files, 1,001 url-encoded fields and a `charset=latin-1`
filename (Python refused the first three with Starlette's 400s where Rust **created the
portfolio**) — and `multipart.rs` now ports Starlette's `max_fields`/`max_files`, `FormParser`'s
limits, the streaming part-size check and `_user_safe_decode`'s charset (R6.21). The file now
grades **270 of 270 EQUAL, side effects EQUAL**, with and without `BASKFY_SOLE_USER_ID`. Left
open: a lone surrogate escape in a JSON body is Rust's 500 against pydantic's 400 `string_unicode`
(the foundation's `body.rs`, R6.7 — not this lane's file).

**Tests.** Unit tests per module, each expectation checked against the live Python (pydantic's int
and Decimal rules, `csv.reader`, python-multipart, `portfolio_csv`, `parse_tradebook_csv` and
`strptime`, `check_move`, `_name_from`, `_requested_quantities`), `tests/portfolio.rs` grading each
write route's verb and 405s, and the order-path source scan over the new files.

## Wave 4c lane writes-sleeves (RS4c, 30 Oct 2026) — 18 of the lane's 20 write routes ported

**Committed at RS4c** (the integration commit). Routers: `swing`, `twt`, `vbt`, `sleeves`, `options`,
`fno`, `overlap`, `backtests`. No route places, modifies or cancels an order or a GTT; none names
the execution package (source-scanned by `no_write_here_reaches_a_broker`).

| Route | State |
|---|---|
| `POST /swing/scan`, `POST /twt/scan`, `POST /vbt/scan` | **ported** (409 in flight, 429 inside the minute, the row, the Celery publish after commit and its `task_id` stamp) |
| `POST /swing/watch`, `PATCH`/`DELETE /swing/watch/{id}` | **ported** (`add_manual`/`annotate`/`reconfirm`/`dismiss`, the trading-calendar expiry, the identity-map echo, R6.27) |
| `PATCH /swing/config`, `PATCH /vbt/config` | **ported** (ceilings first, audit per field, net-change UPDATE) |
| `PUT /portfolios/{id}/sleeves` | **ported** (`SleeveSetIn`, screens by public id, baskets by slug with the sole-tenant rule) |
| `POST`/`DELETE /options/event-day`, `PATCH /options/config` | **ported** (NIFTY-only before-validators, every bound before any write) |
| `PATCH /fno/config` | **ported** (the group bound, the ₹ risk ceiling, the last-group overlay, R6.27) |
| `PUT`/`DELETE /overlap/tags`, `POST /overlap/catalyst-scan` | **ported** (`catalyst_tags` slice, R6.29; the scan's Redis lock, status and publish) |
| `PUT`/`DELETE /overlap/reviews` | **not ported** — need `GET /overlap` (R5.21, R6.29); Python serves them |
| `POST /backtests`, `DELETE /backtests/{id}` | **ported** — `POST` with the Celery executor only; `inline` is a 503 (R6.28, cutover gate) |

**Graded.** `api_diff.py --writes --lane wsleeves --smtp-port 18233 --redis-db-py 10
--redis-db-rs 11 --py-port 18231 --rs-port 18232 --cases tools/parity/api_cases/writes_sleeves.json`:
**205 of 205 responses EQUAL, side effects EQUAL** (`sw_scan_run`, `tw_scan_run`, `vb_scan_run`,
`sw_watch`, `sw_config`(+audit), `vb_config`(+audit), `op_event_day`, `op_book_config`,
`op_sleeve_config`, `op_config_audit`, `fo_book_config`, `fo_sleeve_config`, `fo_config_audit`,
`portfolio_sleeve`, `catalyst_tag_correction`, `backtest`, every sequence, both Redis databases —
the Celery messages, kombu bindings, idempotency records and the wanted set). Covered: no body,
`null`, a list, bad JSON before the 401, every field type's refusals, extras forbidden and ignored,
anonymous/plain/stale-epoch/expired/garbage callers, the sole-tenant refusal, 404s, the
ceilings (422) and engine bounds (400), atomicity, idempotent replays and in-flight keys, the
concurrency caps, and the GETs that show each effect. The GET files `foundation.json`,
`sleeves.json`, `derivatives.json`, `portfolio.json` stay **987 of 987 EQUAL** with
`BASKFY_SOLE_USER_ID=1`. Not graded: a malformed body with a refused token (R6.30, foundation).

**Adversarial review (R6.32).** The decorators count 20 writes (swing 5, twt 1, vbt 2, options 3,
fno 1, overlap 5, backtests 2, sleeves 1): 18 ported, `PUT`/`DELETE /overlap/reviews` excluded.
Two divergences fixed — `POST /backtests` with no broker now fails the row in the request as
Python does, and `casefold` (a stored key) is exact. Re-graded on `--py-port 18331 --rs-port
18332 --redis-db-py 14 --redis-db-rs 15`: `writes_sleeves.json` **217 of 217 EQUAL, side effects
EQUAL** (12 cases added: cross-account sleeves writes and reads, a cross-account backtest read,
the casefold put/delete, rollback beside a refused ceiling in the three config patches), and the
new `writes_sleeves_nobroker.json` (no Redis: the failed backtest, the three scans, the filings
scan's refusal, a tag correction) **9 of 9 EQUAL, side effects EQUAL**. One foundation divergence
recorded for the integrator: Rust's limiter refuses everything without Redis; Python has none.

**Tests.** Unit tests for the validator (Decimal spelling and comparison, error order, speedate
shapes), the Celery message, the routing table, `cache_key`/`tag_headline`/`resolve_tag`, and the
order-path source scan; the sleeve lane's read-only test and `tests/derivatives.rs`' 405 sweep
updated to the write-wave spec (R6.31).

## Wave 4c lane writes-accounts (RS4c, 30 Oct 2026) — 20 of the lane's 20 write routes ported

**Committed at RS4c** (the integration commit). Routers: `api_keys`, `alerts`, `webhook_endpoints`,
`support`, `admin`, `billing`. No route places, modifies or cancels an order or a GTT; none names
the execution package (source-scanned by `no_module_of_this_lane_can_reach_an_order`, the Rust
port of `test_admin_resync.py`'s `TestItCannotPlaceAnOrder`).

| Route | State |
|---|---|
| `POST /keys`, `POST /keys/{id}/rotate`, `POST /keys/{id}/revoke`, `DELETE /keys/{id}` | **ported** (the secret in the create/rotate answer only, never stored or logged; scopes, ceiling, ten-key cap, idempotent revoke, delete-only-revoked; the FK 500 reproduced, R6.38) |
| `POST /alerts`, `PATCH`/`DELETE /alerts/{id}`, `POST /alerts/unsubscribe` | **ported** (visibility, one per screen, derived unsubscribe token, net-change UPDATE; `/alerts/unsubscribe` under PATCH/DELETE as Starlette routes it, R6.38) |
| `POST /webhook-endpoints`, `PATCH`/`DELETE /webhook-endpoints/{id}`, `POST …/rotate-secret` | **ported** (the SSRF guard, R6.35; the derived signing secret; the circuit-breaker reset) |
| `POST /support` | **ported** (both templates byte for byte; `Reply-To` the sender; the 500 when support cannot be mailed) |
| `POST /admin/pipeline/runs/{date}/rerun`, `POST /admin/instruments/{symbol}/reprocess`, `POST /admin/resync` | **ported** (the staff gate, pydantic's `date`, the Celery publish and the audit row, R6.36) |
| `PUT`/`DELETE /admin/users/{id}/entitlements[/{feature}]` | **ported** (upsert with `previous`, the audit row, the effective detail from the transaction) |
| `POST /checkout/session` | **ported** (Razorpay through the guarded client; graded on a closed port, and on a local stub off the record, R6.37) |
| `POST /webhooks/razorpay` | **ported** (signature, dedupe, lifecycle, the captured payment and its GST invoice — number, split, PDF byte for byte, archive; R6.37) |

**Graded.** `api_diff.py --writes --lane wacc --smtp-port 18203 --redis-db-py 8 --redis-db-rs 9
--py-port 18201 --rs-port 18202 --cases tools/parity/api_cases/writes_accounts.json`:
**321 of 321 responses EQUAL, side effects EQUAL** — tables `api_key`, `api_key_usage_daily`,
`screen_alert`, `screen_alert_delivery`, `webhook_endpoint`, `webhook_delivery`, `admin_action`,
`entitlement_override`, `subscription`, `payment`, `invoice_counter`, `webhook_event`, every
sequence, both Redis databases (the Celery messages and kombu bindings), and the six support mails.
Covered: no body, `null`, lists, text bodies, bad JSON before the 401, every field type's refusals
(ints, bools, datetimes, literals, list items, emails, lengths), extras, anonymous/plain/stale/
expired/other-tenant/non-staff callers, 404s, limits (ten keys, ten endpoints), idempotent
revoke/unsubscribe/dedupe, the int32 and FK 500s, and the GETs that show each effect (lists,
usage, deliveries, `/admin/users/{id}`, `/admin/actions`, `/invoices`, the invoice PDF). Off the
record: the checkout success path against a local Razorpay stub (14 of 14, gateway requests
byte-equal) and without keys (10 of 10). The GET files `foundation.json`, `accounts.json`,
`portfolio.json` stay **1,022 of 1,022 EQUAL** with and without `BASKFY_SOLE_USER_ID=1`.

**Tests.** 228 pydantic vectors (`datetime`, `date`), the golden invoice PDF, the SSRF refusals
(`test_webhook_ssrf.py`'s URLs and more), scopes, the derived tokens/secrets, the Celery envelope
and routes, the GST split, the support templates and the order-path source scan;
`cargo test -p baskfy-api`: 285 passed, 0 failed.

**Open:** the lane's pydantic emulation and Celery producer duplicate two other lanes' (R6.33,
R6.36); a naive datetime is stored as UTC (R6.34, for Maulik); `DELETE /keys/{id}` after a
rotation is a Python 500 reproduced (R6.38, for Maulik); the SSRF guard's non-ASCII host
approximation (R6.35).

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
7 PUT); none was registered at RS4b (the pool was read-only, R5.2). **RS4c ported 82 of them**
(section "Wave 4c integration" above; the last column of this table).

In the table below, cases are attributed to the router whose Python route they hit; framework
cases are unrouted paths, the root and decoded-path probes.

| Router | Lane | GET ported / total | Diff cases EQUAL / total | Write routes (wave 4c) |
|---|---|---:|---:|---:|
| `admin` | accounts | 9 / 9 | 93 / 93 | 5 — **5 ported** (writes-accounts) |
| `alerts` | accounts | 2 / 2 | 22 / 22 | 4 — **4 ported** (writes-accounts) |
| `api_keys` | accounts | 2 / 2 | 34 / 34 | 4 — **4 ported** (writes-accounts) |
| `auth` | foundation + accounts | 2 / 2 | 25 / 25 | 6 — **5 ported at RS4c.0, `/auth/google` up to the verifier** (74 writes cases) |
| `backtests` | derivatives | 7 / 7 | 72 / 72 | 2 — **2 ported** (writes-sleeves; `inline` executor 503s, R6.28) |
| `baskets` | sleeves | 2 / 2 | 12 / 12 | 0 |
| `billing` | portfolio | 3 / 3 | 40 / 40 | 2 — **2 ported** (writes-accounts) |
| `brokers` | portfolio | 3 / 3 | 33 / 33 | 2 — **2 ported** up to Kite's live read (writes-portfolio, R6.22) |
| `curated_costs` | accounts | 1 / 1 | 23 / 23 | 0 |
| `curated_create` | accounts | — (no GET) | 0 / 0 | 1 — **1 ported** (writes-screens) |
| `curated_customize` | accounts | — (no GET) | 0 / 0 | 1 — **1 ported** (writes-screens) |
| `curated_drift` | accounts | — (no GET) | 0 / 0 | 2 — **2 ported** (writes-screens) |
| `curated_engage` | accounts | 2 / 2 | 11 / 11 | 2 — **2 ported** (writes-screens) |
| `curated_from_screen` | accounts | — (no GET) | 0 / 0 | 2 — **0 ported**: both run the screener (R6.13) |
| `curated_investments` | accounts | 3 / 3 | 67 / 67 | 3 — **3 ported** (writes-screens) |
| `curated_plans` | accounts | — (no GET) | 0 / 0 | 3 — **3 ported** (writes-screens) |
| `curated_sip` | accounts | 1 / 1 | 26 / 26 | 1 — **1 ported** (writes-screens) |
| `curated_trending` | accounts | 1 / 1 | 6 / 6 | 0 |
| `curated_versions` | accounts | 2 / 2 | 33 / 33 | 0 |
| `desk` | reference | 5 / 5 | 51 / 51 | 0 |
| `explore` | reference | 9 / 9 | 218 / 218 | 2 — **0 ported**: `POST /watchlist`, `DELETE /watchlist/{slug}` were in no lane (R6.40) |
| `fno` | derivatives | 3 / 3 | 16 / 16 | 1 — **1 ported** (writes-sleeves) |
| `instruments` | reference | 6 / 6 | 184 / 184 (rank-history scoped, R6.1) | 0 |
| `kite` | reference | 2 / 2 | 96 / 96 | 0 |
| `managers` | accounts | 2 / 2 | 16 / 16 | 4 — **4 ported** (writes-screens) |
| `market_data` | reference | 4 / 4 | 152 / 152 | 0 |
| `meta` | reference + foundation | 7 / 7 | 55 / 55 | 0 |
| `options` | derivatives | 9 / 9 | 124 / 124 | 3 — **3 ported** (writes-sleeves) |
| `overlap` | derivatives | 3 / 4 — `GET /overlap` not ported (R5.21) | 17 / 17 | 5 — **3 ported**; `PUT`/`DELETE /overlap/reviews` wait for `GET /overlap` (R6.29) |
| `portfolio_overview` | portfolio | 7 / 7 | 118 / 118 | 3 — **3 ported** (writes-portfolio) |
| `portfolios` | portfolio | 6 / 6 | 80 / 80 | 6 — **5 ported**; `/rebalance` runs the screener (R6.23) |
| `public` | reference | 0 / 7 — never mounted (D9) | 6 / 6 | 0 |
| `screens` | accounts | 4 / 4 | 108 / 108 | 8 — **4 ported**; preview, explain, selection, run execute a screen (R6.13) |
| `search` | reference | 1 / 1 | 77 / 77 | 0 |
| `sleeve_state` | sleeves | 1 / 1 | 9 / 9 | 0 |
| `sleeves` | sleeves | 2 / 2 | 25 / 25 | 1 — **1 ported** (writes-sleeves) |
| `support` | — | — (no GET) | 0 / 0 | 1 — **1 ported** (writes-accounts) |
| `swing` | sleeves | 10 / 10 | 113 / 113 | 5 — **5 ported** (writes-sleeves) |
| `track_b` | portfolio | 3 / 3 | 16 / 16 | 3 — **3 ported** (writes-portfolio; 404 while Track B flags stay false) |
| `trades` | portfolio | 1 / 1 | 27 / 27 | 2 — **2 ported**; `sync-today` up to Kite's live read (R6.22) |
| `twt` | sleeves | 3 / 3 | 19 / 19 | 1 — **1 ported** (writes-sleeves) |
| `vbt` | sleeves | 7 / 7 | 41 / 41 | 2 — **2 ported** (writes-sleeves) |
| `watchlist` | accounts | 2 / 2 | 16 / 16 | 3 — **3 ported** (writes-screens) |
| `webhook_endpoints` | accounts | 2 / 2 | 21 / 21 | 4 — **4 ported** (writes-accounts) |
| framework + `/health` (`app.py`) | foundation | 1 / 1 | 15 / 15 | — |
| **total** | | **140 / 141** mounted (+7 public, never mounted) | **2113 / 2113** (2,285 at RS4c) | **94 — 82 ported, 1 partly (`/auth/google`), 11 on Python** (RS4c) |

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

R5.36 lists the residual differences no committed case reaches. R5.28 listed two **Python-side
defects** the port reproduced on purpose (the `/trades` cross-tenant date leak and the
unauthenticated rank-history read); both are fixed at RS4c.0, Python first (R6.1).

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

## Wave D integration (RS-D, 30 Oct 2026, worktree `rs-desk`) — the desk ported module by module, execution core included, DRY_RUN only

Porting is **not** cutover (DECISIONS-RS R8.0, R8.24): the Rust desk has no live Kite client,
no auto-execute path wired (the swing-monitor, twt-auto loop, fno-monitor, options-monitor and
session-supervisor services refuse to start), and cuts over only after **two shadow Fridays in
DRY_RUN with byte-equal plans and Maulik's explicit go**.

**Gates (this worktree's `rust/`):** `cargo fmt --check` clean; `cargo clippy --workspace
--all-targets -D warnings` clean; `cargo test --workspace` with `BASKFY_RS_TEST_DATABASE_URL` on
the bed: **83 targets, 1,864 passed, 0 failed, 19 ignored** (every golden green).
`baskfy-desk` alone: 17 targets, 851 passed, 5 ignored (lib 569; exec_gateway 36,
exec_gateway_gtt 30, exec_gateway_exit_rules 3, exec_kite 38, non_negotiables 7,
options_lane 22, options_confirm_db 14 — these run only with `BASKFY_RS_OPTIONS_TEST_PG`, else
skip as Python's `requires_db` — twt_desk 21, vbt_desk 44, weekly_plan 23, weekly_engine 13,
settings 7, storage 7, path_prefix 9, engines 5, fake_kite 3).

**desk_diff (Python vs Rust, same replay Kite, same clock, same ids):** one run over **every** `desk_cases/*.json` (24 files, in
file order on one Postgres bed clone) plus `--plans`: **485 of 491 cases EQUAL**, and **every side
effect EQUAL** — the replay Kite's **order/GTT calls** (none on either side), every file under
`data/` (journals, execution logs, retained scans), all 28 touched tables (`desk.*`,
`public.risk_ledger`, `sw_*`, `tw_*`, `vb_*`; only server `now()` columns masked), Redis and
mail. `--plans`: all 6 `uploads/*.csv` give byte-equal `/analyze` bodies (2 plans, 4 equal 500s).
Variants: `session_variants/refused.json` on `desk_fixtures/session_refused` 4/4;
`no_key.json` + `swing_desk.json` with `KITE_API_KEY=` 46/46, 0 differences. **The 6 DIFFs
(all residuals, none on an order path):** `/regime/` and `/regime/backtest/` (Starlette's
`redirect_slashes` 307 vs Rust 404 — app-wide fallback owed, R8.23); `/performance` and
`/performance/data` (not ported, R8.22); two `generate_for` cases where a retained scan from an
earlier file makes Python attempt the generated scan, which Rust answers 503 "not ported"
(R8.12). **Plan kinds:** only the weekly `/analyze` has a builder; swing MORNING/LIVE, twt
MORNING/EVENING/LIVE, vbt, fno and options plans have **no** `plans_*.json` builder case (the bed's
own plans are exercised by the lanes' confirm cases instead). Integration fixes in this run:
boot applies stored settings and installs the badge state (R8.8); the harness normalises each
side's run directory in bodies (`<run>`, like the port) — the settings page printed
`KITE_TOKEN_FILE` under `…/py` vs `…/rs` — and `DESK_DIFF_DUMP=<dir>` writes full differing
bodies.

| Module (Python → Rust) | State | Rust tests | desk_diff cases (lane run) |
|---|---|---|---|
| `baskfy_execution/gateway.py` + `app/core/gateway.py` → `exec_core/{order_gateway,journal,desk}.rs` | ported | 36 + 30 GTT | 12/12 EQUAL (sqlite and postgres) |
| `app/exit_rules.py` → `exec_core/exit_rules.rs` | ported | 3 | — |
| `guards.py`, `ratelimit.py`, `kite_limits.py`, `risk.py`/`risk_store.py`, `net.py`, `websec.py` → `exec_core/*` + `websec.rs` | ported | 93 | 27/27 |
| `app/kite_client.py`, `app/token_store.py` → `exec_core/{kite_client,token_store}.rs` | ported (`place_cnc_order` deliberately not, R8.9); D2: `kite()` refusal on every method, Python's exceptions on malformed replies (175 goldens), `.env` key, `/status` helper `exec_kite::status_session` awaiting the session lane's wiring (R8.11.1) | 50 + 175 goldens + 1 | 8/8 |
| `app/lifecycle.py` → `exec_core/lifecycle.rs` | ported (D2, R8.9.1) | 18 | `exec_gateway_lifecycle.json` + 2 variants |
| `main.py` `/analyze`, `/execute`, `plan_store.py`, `rebalance.py`/`scoring.py`, `strategies/*`, `breadth_source.py` → `weekly/*` | ported; generated scan answers 503 (R8.12) | 36 + 4 | 20/20 + `--plans` |
| `holdings_view`, `tax_lots`, `tradebook`, `reconcile_view`, `reconcile`, `snapshot.collect` → `weekly/weekly_books/*` | ported, graded by 23 Python goldens | 92 | — (no own routes) |
| `swing_desk.py`, `strategies/swing_breakout.py`, `breadth_source.py` → `swing/{desk,breakout,breadth}.rs` | ported | 30 + 13 + 3 | 42/42 (pg, sqlite, no-key) |
| `swing_execute.py`, `swing_live_gate.py` → `swing/swing_exec/*` | ported | 61 | 14/14 |
| `swing_monitor.py`, `swing_clock.py`, `swing_notify.py` → `swing/swing_monitor/*`, `notify.rs` | ported; service wired (R8.SM1), SMTP/Telegram sent (R8.SM3), Python `run_chore` fixed (R8.SM2) | 141 + 4 | 8/8 + 7 SIGNAL plans |
| `twt_desk.py` → `twt.rs`, `twt/{store,common}.rs` | ported | 21 | 55/55 |
| `twt_execute.py`, `twt_auto.py` → `twt/{execute,auto}.rs` | ported; twt-auto loop script not ported | 35 + real-path 1 | 42/42 |
| `vbt_desk.py`, `vbt_execute.py` → `vbt.rs`, `vbt/*` | ported | 44 | 27/27 (+ edges, refusals) |
| `fno_gates`, `fno_clock`, `fno_ledger`, `fno_desk.py` → `fno/*` | ported; D2: the DB halves of `test_fno_desk`/`test_fno_ledger` (+ F3's route halves) in `tests/fno_desk_db.rs` against the rows Python's own monitor/executor/ledger wrote (`fixtures/fno/goldens.json`), quotes raise as Python's, `fno-monitor` off is Ok (R8.19.1) | 18 + 40 DB | 23/23 + `fno_world.json` (needs `--fixture desk_fixtures/fno_world`) + `plans_fno.json` |
| `fno_execute.py`, `fno_monitor.py` (order half) → `fno/fno_exec/*` | ported; monitor's planning/watch/night half **not** | 12 + 4 safety | 18/18 |
| `options_gates/lab/desk/execute/ledger/clock`, `intraday_skeleton` → `options/*` | ported; `strategies/nifty_options.py` **not** (R8.20 F5) | 22 + 14 DB | 28/28 |
| `core/regime.py` (config), `regime_store` (reads), `breadth`, `index_cache` (reads), `regime_view` → `analytics/*` | ported; engine/`regime_run`/`regime_alloc`, `/indices*` **not** | 23 | 21/22 (trailing slash) |
| `protection`, `corporate_actions`, `autorun` (equity half), `backtest_view` → `analytics/analytics_views/*` | ported | 91 | 6/9 (trailing slash, `/performance` ×2) |
| `settings.py`, `ops.py`, `daily_runs.py` → `analytics/{desk_settings,ops,daily_runs}.rs` | ported (coarse run, kept) | in lib | views.json |
| `metrics.py`, `benchmark.py`, `performance_view.py` | **not ported** (`/performance*` 404) | 0 | — |
| `session_supervisor.py`, `telemetry.py`, `core/ticker.py`, `/callback`, `/status`, `/login`, static | ported; supervisor **refuses** without a reconciler | 34 | 54/54 + variants 4/4, 4/4 |

**Python desk tests not yet ported** (of 2,315 test functions; counts from the lanes): exec_core —
none of the execution package's files (D2, R8.9.1); route parts of `test_execute_gateway.py`
and `test_kite_limits` source scans are the exec_guards lane's; weekly — `test_generated_scan.py`
(15), two `test_plan_store` slippage tests, 10 of `test_reconcile.py`'s 21 (sleeve handlers);
analytics — `test_metrics` (58), `test_benchmark` (20), `test_drawdown` (14), `test_xirr` (14),
`test_twr_chaining` (13), `test_regime.py`/`test_regime_plan.py` engine tests, `test_index_board`
(24), 13 of `test_autorun` (25); swing — 84 of `test_swing_desk.py` (114), ~60 of
`test_swing_execute.py` (122), `test_swing_track_c.py` (22), ~82 of the 121 monitor/clock/notify
tests, `test_swing_monitor_schedule` (10); twt — `test_twt_execute.py` entry/sweep/reconcile/TW7
(~50 of 61), `TestTheLoop`; vbt — ~65 of 109 (store round-trips, HTML unit, route enumeration);
fno — `test_fno_monitor` (19), `test_fno_monitor_loop` (8), `test_fno_f3` (17), DB halves of
`test_fno_safety` (those of `test_fno_ledger`/`test_fno_desk` are D2's, R8.19.1); options — `test_options_monitor` (24),
`test_options_monitor_loop` (7); session — `test_token_sync.py` (11),
two `test_telemetry` order-path tests; Python-only by design — `test_deploy_kit`,
`test_requirements`, `test_frozen_boundary`.

### Safety audit (RS-D integration, 30 Oct 2026)

1. **Every call of the Kite order/GTT transport** (`KiteWrites::{place_order, modify_order,
   cancel_order, place_gtt, modify_gtt, delete_gtt}`) in `baskfy-desk` non-test code — seven
   sites, all inside `lanes/exec_core`:
   - `exec_core/order_gateway.rs:505` `place_order`, `:815` `place_gtt`, `:974` `delete_gtt`,
     `:1233` `modify_gtt`, `:1365` `cancel_order` — each only after tenant → untouchables →
     overnight/FO cover → product gates → risk → idempotency → rate limit, and only when the
     order is not DRY_RUN (the DRY_RUN branch journals `dry_run` and returns).
   - `exec_core/kite_client.rs:727` `delete_gtt`, `:775` `place_gtt` — **the documented GTT
     exception** (root CLAUDE.md non-negotiable 6's caveat): `DeskKite::place_gtt_stop` /
     `delete_gtt` run `assert_tradeable` (and the overnight-option guard on place) and read DRY_RUN
     from the shared settings **at each call**, before the broker.
   The trait is named only in `kite.rs`, `fake_kite.rs`, `app.rs` (`StateParts`), `main.rs`
   (`NoKite`, which refuses every write) and `exec_core.rs` (the crate-private `Broker` alias);
   `tests/non_negotiables.rs` enforces it. Every other order/GTT call in the crate (`swing_exec`,
   `twt`, `vbt`, `fno_exec`, `options`, weekly `/execute`) is a `Gateway` trait method on a
   gateway derived from `DeskGateway`. The binary holds **no live Kite client**: without
   `--harness` it is `NoKite`; `--harness` refuses to start with `DRY_RUN=false`.
2. **Flags default false in code** (`src/settings.rs`, `Kind::BoolEqTrue`, default `"false"`):
   `BASKFY_SWING_AUTO_EXECUTE`, `BASKFY_TWT_AUTO_EXECUTE`, `BASKFY_FNO_F3_AUTO_EXIT` (the only
   three auto flags — a fourth fails `tests/settings.rs`), `BASKFY_SWING/TWT/VBT_EXECUTION_ENABLED`,
   `BASKFY_FNO_F1/F2/F3_EXECUTION_ENABLED`, `BASKFY_OPTIONS_O1M/O1W/O2/O3_EXECUTION_ENABLED`,
   `BASKFY_FNO_CARRY_ENABLED`, `INTRADAY_ENABLED`, `OPTIONS_ENABLED`, `BASKFY_FNO_MONITOR_ENABLED`,
   `BASKFY_OPTIONS_MONITOR_ENABLED`. **`DRY_RUN` defaults true** (`Kind::BoolNeFalse`: off only for
   the exact string `false`). No option has an auto flag; the weekly rebalancer and VBT have none.
3. **No network outside the guarded client.** The crate depends on no HTTP client; the one socket
   it opens is `lanes/session.rs` `redis_ping` (the shared limiter's Redis PING for `/status`). No
   Kite, NSE or other endpoint was called by any run in this wave (one lane's `/status?ip=1` case
   made the Python desk do an outbound-IP lookup once; the case was removed).
4. **No secret in logs.** The replay log redacts the API key and access token; operator log lines
   carry symbols, kinds, statuses and refusal words only; no `.env` from the live stack was read.
5. **`frozen/strangle` untouched** (`git diff HEAD -- frozen` empty); the Python desk and
   `decile-blueprint` trees are unchanged in the worktree.
6. **`portfolio.db` never opened.** Every `portfolio.db` path in the crate, the harness and the
   goldens scripts is a fresh file under a temporary run directory (`desk_diff.py`'s sqlite
   backend) or the code default string; the main tree's file is unchanged (mtime 22 Aug 2026). A
   SQLite `data/portfolio.db` created in the **worktree** by a lane's Python import (git-ignored)
   is not the evidence file.
7. **Databases:** no `rsd_*` database left (two orphaned clones from an earlier lane run and the two of a killed integrator run were dropped after checking only TimescaleDB's scheduler held them); `baskfy` unchanged (its only non-zero write counter is TimescaleDB's own `_timescaledb_internal.bgw_job_stat`). Template `baskfy` is never written (clones only).
8. **Ports 18500–18799:** no listener left (`lsof` after the last run: 0); orphaned desks from a killed run were stopped.

## Wave D foundation (30 Oct 2026, worktree `rs-desk`) — `baskfy-desk`, the replay Kite, `desk_diff.py`

Wave D ports the desk (`kite-momentum-rebalancer/app`) in parallel with the worker, **execution
core included** — porting is not cutover (DECISIONS-RS R8.0). The foundation is in; every lane
is a declared stub.

| Piece | State |
|---|---|
| `rust/crates/baskfy-desk` | lib + binary; settings (113 env vars + 6 function-level, held to the live Python's defaults and 18 probes each), storage (Postgres `desk` schema with psycopg's literal typing, SQLite), clock/ids seams, `KiteReads`/`KiteWrites` (law 2 in the types), `Gateway` trait + client ids/tenancy/gates/stop band, plans + the confirm gate, minijinja templates (Jinja2-exact on every probe), orjson wire, the axum skeleton (`DeskSecurity`, `PathPrefix`) — R8.1–R8.4 |
| Replay Kite | `fake_kite.rs` + `tools/parity/desk_fake_kite.py`, one fixture (`desk_fixtures/default`, synthetic from the corpus), identical call logs — R8.3 |
| `tools/parity/desk_diff.py` | both desks, both backends, all side effects; **Python-vs-Python 17/17 EQUAL + every side effect EQUAL on Postgres and on SQLite**; Python-vs-Rust: 5 foundation cases EQUAL, the weekly scenario waits for its lane — R8.6 |
| Tests | `cargo test -p baskfy-desk`: 7 targets, 38 tests (settings 7, non-negotiables 7, storage 7, engines 5, path prefix 9, replay 3) |
| Lanes | `exec_core` (`NotPortedGateway` refuses everything), `weekly`, `analytics`, `swing`, `twt`, `vbt`, `fno`, `options`, `session` — stubs in `src/lanes/` |

**Porting checklist — the desk's Python tests by lane** (test functions before parametrisation;
`kite-momentum-rebalancer/tests/` plus `decile-blueprint/packages/execution/tests/` for
`exec_core`). Each lane ports its tests first (the non-negotiables before the code they guard),
then its routes, graded by `desk_diff.py` with `desk_cases/<lane>.json`:

| Lane | Routes | Services | Test files | Test functions | Files (functions) |
|---|---|---|---|---|---|
| `exec_core` | 0 | 0 | 22 | 314 | `test_execute_gateway.py` (40), `test_guards.py` (20), `test_seven_non_negotiables.py` (12), `test_ratelimit.py` (7), `test_kite_limits.py` (39), `test_risk_store.py` (3), `test_outbound_ip.py` (6), `execution/test_af_lane_e.py` (11), `execution/test_broker_adapters.py` (6), `execution/test_broker_ports.py` (2), `execution/test_client_id_journal.py` (21), `execution/test_derivative_product_gate.py` (10), `execution/test_fno_carry_gate.py` (13), `execution/test_fno_covered_overnight.py` (16), `execution/test_fno_gtt_trail_modify.py` (6), `execution/test_gtt_gateway.py` (43), `execution/test_gtt_limit_fraction.py` (6), `execution/test_gtt_modify_and_cancel.py` (18), `execution/test_non_negotiables.py` (17), `execution/test_risk_store.py` (9), `execution/test_stop_from_vol.py` (6), `execution/test_tenant_isolation.py` (3) |
| `weekly` | 7 | 0 | 17 | 237 | `test_plan_store.py` (30), `test_stops_route.py` (21), `test_protection.py` (41), `test_rebalance_pricing.py` (12), `test_no_trade_band.py` (12), `test_generated_scan.py` (15), `test_breadth_and_shadow.py` (15), `test_costs.py` (14), `test_execution_report.py` (12), `test_holdings_same_day.py` (15), `test_reconcile.py` (21), `test_reconcile_view.py` (12), `test_desk_score_parity.py` (3), `test_desk_config_parity.py` (4), `test_scoring_moved_to_core.py` (2), `test_basket_moved_to_core.py` (4), `test_exposure_moved_to_core.py` (4) |
| `analytics` | 24 | 0 | 24 | 818 | `test_metrics.py` (58), `test_benchmark.py` (20), `test_drawdown.py` (14), `test_xirr.py` (14), `test_twr_chaining.py` (13), `test_tradebook.py` (40), `test_corporate_actions.py` (23), `test_db_snapshots.py` (19), `test_daily_runs.py` (27), `test_desk_daily_loop.py` (20), `test_autorun.py` (25), `test_ops.py` (29), `test_settings.py` (40), `test_settings_boundary.py` (15), `test_regime.py` (97), `test_regime_data.py` (72), `test_regime_plan.py` (60), `test_regime_view_backtest.py` (96), `test_index_board.py` (24), `test_holdings_view.py` (14), `test_backtest_view.py` (63), `test_pages.py` (20), `test_backup.py` (9), `test_exit_codes.py` (6) |
| `swing` | 7 | 1 | 9 | 386 | `test_swing_desk.py` (114), `test_swing_execute.py` (122), `test_swing_monitor.py` (84), `test_swing_monitor_schedule.py` (10), `test_swing_clock.py` (15), `test_swing_notify.py` (12), `test_swing_track_c.py` (22), `test_swing_budgets.py` (2), `test_swing_catalyst_desk.py` (5) |
| `twt` | 9 | 1 | 4 | 153 | `test_twt_desk.py` (40), `test_twt_execute.py` (61), `test_twt_auto.py` (35), `test_twt_scan_desk.py` (17) |
| `vbt` | 6 | 0 | 5 | 109 | `test_vbt_desk.py` (22), `test_vbt_execute.py` (33), `test_vbt_rescan_desk.py` (29), `test_vbt_safety.py` (13), `test_vbt_scan_safety.py` (12) |
| `fno` | 3 | 1 | 8 | 97 | `test_fno_desk.py` (19), `test_fno_f3.py` (17), `test_fno_gates.py` (5), `test_fno_ledger.py` (19), `test_fno_monitor.py` (19), `test_fno_monitor_loop.py` (8), `test_fno_safety.py` (6), `test_fno_deploy.py` (4) |
| `options` | 4 | 1 | 8 | 71 | `test_options_execute.py` (15), `test_options_gates.py` (4), `test_options_hardening.py` (4), `test_options_ledger.py` (5), `test_options_monitor.py` (24), `test_options_monitor_loop.py` (7), `test_options_safety.py` (11), `test_options_drill.py` (1) |
| `session` | 7 | 1 | 8 | 103 | `test_session_supervisor.py` (18), `test_lifecycle.py` (17), `test_token_sync.py` (11), `test_telemetry.py` (12), `test_websec.py` (16), `test_path_prefix.py` (9), `test_broker_faces.py` (3), `test_pg_adapter.py` (17) |
| *Python-only (not ported)* | — | — | 3 | 27 | `test_deploy_kit.py` (21), `test_requirements.py` (3), `test_frozen_boundary.py` (3) |
| **all** | 67 (66 + `/static`) | 5 | 108 | 2315 | |


**Not done (loud):** no route ported; no live Kite client (cutover item, R8.0); the static
handler, startup (`_apply_stored_settings`, migrations at boot, secret-permission check) and
`/status` are the session lane's; plan builders for swing/twt/vbt/fno have no case file yet;
`time.monotonic` is not frozen in the Python harness (Kite *read* counts and the limiter's
Redis clocks are reported, not graded). Cutover of anything desk-side needs two shadow Fridays
and Maulik's explicit go.

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

**Done at RS4c:** wave 4c, the write routes — 82 of 94 ported, 1 partly, 11 on Python with
reasons (section "Wave 4c integration"). Before any write route cuts over: the body-before-token
order (R6.17/R6.30), Google's verifier (R6.3), the mailer's STARTTLS/AUTH and Resend (R6.6), and
the collapse of the duplicated validators and Celery producers.

**Done at RS5:** the nightly in Rust, all 14 steps, every `--chain` scenario EQUAL (section "Wave 5 integration").

**Next — the worker's remaining Celery tasks** (screen runs with R6.13's routes, backtests, sleeves' evening/premarket jobs, holdings sync read-only, alerts), then the desk. Wave 5 began with the nightly (step 4 below): catch-up,
publish and the quality gate, graded by `pipeline_run_step` rows equal to the Python run's on a
bed clone (the writes harness's clones, R6.5, are the starting point). The screener port the 11
remaining API write routes need (R6.13) is shared with the worker's screen runs and lands with it.

Never a route that places, modifies or cancels an order (law 2: execution is the last wave).

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
| `baskfy_core.adjustments` | 384 | ported in the worker (`steps/ingest/adjust.rs`, lane nightly-ingest, R7.11) |
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

### Wave 5 lane **compute** (steps 7, 8 and the desk score; DECISIONS-RS R7.12) — not committed

| Python | Rust | state |
|---|---|---|
| `tasks/factors`, `engine` (load_history, levels, fundamentals, true ATH) | `steps/compute/factors.rs` | ported |
| `tasks/ranking.run_rank_columns`, `factors_ranking.cross_sectional_pctile`/`rank_persistence` | `steps/compute/ranking.rs` | ported |
| `tasks/desk_score`, `desk_score_service.score_day`, `momentum_scan.build` (scan columns), `score.score` | `steps/compute/desk.rs`, `score.rs` | ported |
| `tasks/market_health`, `breadth.breadth_query` | `steps/compute/market_health.rs` | ported |

`nightly_diff.py` (py,rs; 2026-10-29 replay day), all **EQUAL** (tables, `pipeline_run_step` rows incl. detail
JSON, sequences, Redis, mail): `--step compute_factors` fresh (`factor_daily` 928,614 rows, `desk_score_daily`
218,553), rerun, gate-refuse; `--step compute_market_health` fresh, rerun, gate-refuse (`market_health_daily`
4,260). Unit tests: 22 in the lane (Python's step-level assertions: lookback arithmetic, mom_pctile/persistence
rules, risk cut, filters) plus an ignored differential of `score` against Python (40 generated scans, 0 diffs).

### Wave 6 lane `twt_jobs` (30 Oct 2026)

All five TWT tasks ported (`detect`, `evening`, `morning`, `scan`, `scan_publish`);
`task_diff.py --cases tools/parity/task_cases/twt_jobs.json`: **30 of 30 EQUAL py,rs**.
Lane unit tests 25 pass, scratch-DB tests 10 pass. Not covered by parity: a live scan that
signals (DECISIONS-RS R8.twt_jobs). Open for the integrator: `task_registry.rs`'s stub count.

### Wave 6 lane `sleeve_backtests` (30 Oct 2026; DECISIONS-RS R8.sb1)

- `vbt.backtest`, `swing.backtest` ported (the swing engine `swing.backtest`+`swing.journal` ported in the worker, graded end to end; no goldens yet). TWT backtest ported (`run_twt_backtest`, no Celery task; `twt_backtest_diff.py`). `options.backtest` **not ported** (~6k lines of unported options core).
- Results: see the case files `sleeve_backtests.json` / `sleeve_backtests_twt.json` (final counts in the lane report).
- For Maulik: the swing backtest still reads `nifty-500` (not SW29's smallcap-250); a ≤2-session VBT window raises `DBAPIError` on a NaN in JSON (Python defect, reproduced).

### Wave 6 lane `sleeve_backtests` (30 Oct 2026; DECISIONS-RS R8.sb1)

`swing.backtest` and `vbt.backtest` ported (the swing engine `baskfy_core.swing.backtest` and
`swing.journal` ported with it, in the worker, graded end to end); TWT's `run_twt_backtest`
ported as a pub function (no Celery task in Python) with `examples/twt_backtest.rs` and
`tools/parity/twt_backtest_diff.py`. `options.backtest` **not ported** (≈6,000 lines of
unported options core; R8.sb1). Parity, `task_diff.py --cases task_cases/sleeve_backtests.json`
py,rs: vbt 9 of 9 EQUAL (one after the NaN fix, run separately); swing 5 of 5 run EQUAL
(7 months of trading with 16 trades, sleeve/cost override, error row, empty window, bad sleeve) —
the year-long, end-defaults-to-today, positional, bad-date and no-user swing cases are written
but were not run (CODE FIRST, 30 Oct). TWT 2 of 5 EQUAL: the three run cases stored EQUAL rows
(params/stats/drift jsonb) but the payload differed in `by_reason`/`skipped` key order; fixed
(Python's dict order restored) and **not re-run**. Lane unit tests 19 pass, scratch-DB tests 2
pass. Open for the integrator: `tests/task_registry.rs`'s stub count (`REGISTRY.len() - 4`).

### Wave 6 lane `vbt_jobs` (30 Oct 2026)

* 9 of 9 VBT tasks ported. The worker compiles, the lane is clean under clippy -D warnings, and its 43 tests pass (26 unit, 17 scratch-DB).
* **Parity not run:** the 57 cases in `task_cases/vbt_jobs.json` are written but have not been graded.
* Open for the integrator: `task_registry.rs::every_stub_refuses_with_task_not_ported` (stub count), and a clippy error in `tasks/api_routes.rs:71` (another lane's file).

* **Wave 6 lane `portfolio` (30 Oct 2026):** `baskfy.portfolio.eod_nav` and `baskfy.pipeline.capture_kite_trades` ported (`tasks/portfolio.rs`, `tasks/portfolio/{nav,ledger,capture,db_tests}.rs`, one additive pub item in `baskfy-api` `routes::trades`); `task_cases/portfolio.json` 21 of 21 EQUAL py,rs; lane tests 14 of 14 (8 on a scratch database). DECISIONS-RS R8.pf1-R8.pf4, UNREVIEWED.

### Wave 6 lane `ops_alerts` (30 Oct 2026; DECISIONS-RS R8.oa1–R8.oa6)

* **6 of 6 tasks ported** (`tasks/ops_alerts.rs` + `ops_alerts/{alerting,checks,webhooks,login_nudge,kite_session}.rs`): `ops.reap_abandoned_runs`, `ops.check_publish_deadline`, `ops.check_kite_token` (once per stored token per IST day, Redis `SET NX`, fail open), `ops.check_queue_backlog`, `alerts.sweep_webhooks` (SSRF re-check, signed POST, backoff, endpoint disabling), `kite.login_nudge` (the API's own login-URL builder, `O_EXCL` marker); and `refresh_kite_session_quietly` (`kite_session_cli.refresh_quietly`) in full outside the harness.
* Additive items elsewhere: `baskfy-worker/Cargo.toml` `reqwest`, `hmac` (workspace pins, no new crate); `baskfy-api` `routes::brokers::{kite_login_url_for_worker, STATE_VALID_FOR_MINUTES}`, `routes::webhook_endpoints::{assert_public_webhook_url_with, webhook_host_encodable, WebhookUrlRefusal}` (+ private helpers in `brokers/writes.rs`, `webhook_endpoints/ssrf.rs`). No existing body changed.
* Tests: 42 lane unit tests (ports of `test_ops_and_alerts.py`, `test_kite_login_nudge.py`, `test_kite_session_bridge.py`, `test_alert_tasks.py` where they are not Celery-config checks); clippy `-D warnings` clean on `baskfy-worker` and `baskfy-api`. `cargo test -p baskfy-worker`: all green except the foundation's `every_stub_refuses_with_task_not_ported` count (`REGISTRY.len() - 4`), which fails for every lane that ports anything (see R8.curated) — **for the integrator**.
* Not ported here (R8.oa6): `action_recovery` (needs `baskfy_core.action_recovery` with goldens first); `purge_accounts` is `api_routes`'; `window`/`calendar`/`fundamentals`/`listings` are wave 5's; `telemetry` is R7.4.
* `task_cases/ops_alerts.json`: 37 cases. `backlog-over-threshold` needs Redis index 5 pre-seeded (`RPUSH default x x x; RPUSH compute x x; RPUSH ingest x`).
