# Rust rewrite — status

Live status of the port (README.md for the plan, DECISIONS-RS.md for the calls). Updated by
whoever changes a module's state; loud about what is **not** done.

**States:** `not started` · `goldens exist (n)` — the answer key is in `go/testdata/golden`,
no Rust yet · `declared` — the Rust module file exists (empty) · `ported` — Rust compiles and
its golden tests run · `verified` — every golden for the module green, clippy clean, reviewed.

## Foundation (29 Oct 2026)

| Item | State |
|---|---|
| `rust/` workspace (edition 2024, resolver 3, toolchain 1.99.1) | done |
| `baskfy-core` crate, module stubs, `CoreError` | done |
| `baskfy-core/tests/law_one.rs` (law 1 in Rust) | green |
| `baskfy-testkit` (load, decode, encode, grade) | done |
| Harness proof: all 455 goldens parse and decode; 365 polars frames round-trip exactly (RS1) | green |
| `cargo fmt` / `clippy --all-targets -D warnings` | green (RS1) |
| `cargo test` | **red: 1 test** — `build_entries_case_010_predates_lv10_pyramiding` (stale golden, see below) |
| providers, execution, api (axum), worker, desk, single binary | not started |

## Wave 1 (RS1, 29 Oct 2026) — swing and options ported

Graded per function against `go/testdata/golden/L1/{swing,options}` by
`rust/crates/baskfy-core/tests/golden_{swing_a,swing_b,options}.rs`. Each area was also checked
by a differential fuzz against the live Python (thousands of random cases); the bugs it found
are fixed and listed in DECISIONS-RS R1.x.

| Python function | Rust | goldens | passing |
|---|---|---:|---:|
| `swing.market.exposure_tier` | `swing::market` | 14 | 14 |
| `swing.opening_range.evaluate_trigger` | `swing::opening_range` | 12 | 12 |
| `swing.sizing.size_position` | `swing::sizing` | 11 | 11 |
| `swing.stops.manage` | `swing::stops` | 16 | 16 |
| `swing.setups.detect_setups` (over `with_swing_indicators`) | `swing::setups` | 10 | 10 |
| `swing.plan.build_entries` | `swing::plan` | 16 | **15** — `build_entries.case_010` red |
| `options.condor.exit_decision` | `options::condor` | 8 | 8 |
| `options.costs.charges` | `options::costs` | 4 | 4 |
| `options.execution.never_naked` | `options::execution` | 6 | 6 |
| `options.execution.simulate_fill` | `options::execution` | 6 | 6 |
| `options.gating.options_gates` | `options::gating` | 20 | 20 |
| `options.ledger.journal_figures` | `options::ledger` | 3 | 3 |
| **total** | | **126** | **125** |

**What is red, exactly.**

* **Rust: `L1/swing/build_entries.case_010`.** The golden predates LV10 (669cf4b, Maulik's
  pyramiding decision of 28 Sep 2026): it expects `ALREADY_HELD` for `HELD`; the live Python
  and the Rust port both give `BUY HELD 1250` and skip `LOCKED_UPPER_CIRCUIT`. Its own test
  fails by design and is not filtered out. Fix: regenerate the swing goldens from the live
  Python (`tools/parity/golden.py swing`) — a golden edit, so not done by an agent (R1.1).
* **Python: `test_swing_goldens.py` is red too — 13 of 128 (`build_entries.case_001`–`005`,
  `007`, `010`–`016`).** Unchanged by RS1 (no Python and no swing golden was touched): the live
  `build_entries` drifted from its goldens at LV10 — it now emits `"position_id": null` on every
  line, and case_010's decision changed. The Rust port keeps the pre-LV10 shape
  (`skip_serializing_if` on `PlanLine.position_id`) so 15 of 16 still match the committed key;
  that attribute is dropped when the goldens are regenerated (R1.1). `test_options_goldens.py`:
  all green.

**Parity limits found (no golden is red because of them, yet).**

* `rust_decimal` holds scale ≤ 28; Python prints a 28-significant-digit quotient below 0.1 at
  scale 29. `options::execution::simulate_fill` on e.g. fills 0.05/0.10 over 3 units is one
  digit short (value within 1e-28), and `L1/backtest/year_fraction.case_004` expects
  `"-0.07939767282683093771389459274"`, which `rust_decimal` cannot print. Needs a decision
  before backtest is ported (R1.3).
* Factor regime columns ignore `as_of` in Python (house rule 5) — recorded as `known_bug` on
  `factors.compute_factors.case_005` and `compute_factors_unrounded.case_003` (R1.4).

## New answer keys (RS1) — ready for wave 2

| Area | Dumper | goldens | functions | byte-stable |
|---|---|---:|---:|---|
| `backtest` + `backtest_metrics` → `L1/backtest/` | `tools/parity/golden_backtest.py` | 139 | 20 | yes (two runs, same shasum) |
| `factors`, `factor_registry`, `screener` (pure query builder), `ranking_engine` → `L1/ranking/` | `tools/parity/golden_ranking.py` | 190 | 28 | yes (three runs incl. `PYTHONHASHSEED=12345`) |

Run either with `cd decile-blueprint && PYTHONPATH=packages/core/src .venv/bin/python
../tools/parity/golden_<area>.py` — the venv's editable `.pth` still points at the old
checkout path; `uv sync` would fix it for every lane.

**Testkit gaps wave 2 must close first:** the ranking goldens use **pandas** frame shapes —
outputs `{columns, dtypes, index, rows}`, inputs `{columns, date_columns, rows}` or
`{columns, rows}` — which `decode_frame` does not read (they decode as plain maps today); a
pandas-shape decoder/encoder is needed before `ranking_engine` can be graded. Errors are stored
as `{"error": {...}}` (backtest) and `{"raises": {...}}` (ranking).

## Wave 2 — recommendation

1. **Regenerate the swing goldens** (Maulik or a golden-owner step, R1.1) so Rust and Python go
   fully green; drop `skip_serializing_if` on `PlanLine.position_id` in the same change.
2. **Settle R1.3** (scale-29 decimals) before porting `backtest`.
3. Port in parallel lanes: **backtest_metrics** (pure numeric, 48 cases) → **backtest** helpers
   (calendar/schedule/weights/CostSpec/PricePanel/PointInTimeReader, 71 cases) →
   `run_backtest`/`run_fragility` (25 cases); and **factor_registry** + **screener** (pure
   text/SQL, 90 cases) → testkit pandas decoder → **factors** (15) → **ranking_engine** (85).
4. Then `twt`/`vbt` (Python already has `test_twt_goldens.py`/`test_vbt_goldens.py`; dump them
   into `go/testdata/golden` the same way) and the `fno` package.

## `baskfy_core` — every Python module

Line counts are `wc -l` of the Python file (comments and docstrings included), measured
29 Oct 2026. Rust modules declared in `baskfy-core` today: `swing::{config, indicators, market,
opening_range, plan, setups, sizing, stops}`, `options::{condor, config, costs, execution,
gating, ledger}`.

| Python module | lines | state |
|---|---:|---|
| `baskfy_core` | 3 | not started |
| `baskfy_core.action_recovery` | 229 | not started |
| `baskfy_core.adjustments` | 384 | not started |
| `baskfy_core.allocation_ledger` | 978 | not started |
| `baskfy_core.api_keys` | 178 | not started |
| `baskfy_core.backtest` | 1927 | goldens exist (L1/backtest), not ported |
| `baskfy_core.backtest_metrics` | 780 | goldens exist (L1/backtest), not ported |
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
| `baskfy_core.factor_registry` | 1214 | goldens exist (L1/ranking), not ported |
| `baskfy_core.factors` | 672 | goldens exist (L1/ranking), not ported |
| `baskfy_core.factors_ranking` | 872 | not started |
| `baskfy_core.fno` | 8 | not started |
| `baskfy_core.fno.calendar` | 148 | not started |
| `baskfy_core.fno.checklist` | 284 | not started |
| `baskfy_core.fno.condor` | 274 | not started |
| `baskfy_core.fno.config` | 486 | not started |
| `baskfy_core.fno.costs` | 131 | not started |
| `baskfy_core.fno.covered` | 125 | not started |
| `baskfy_core.fno.directional` | 639 | not started |
| `baskfy_core.fno.directional_retest` | 495 | not started |
| `baskfy_core.fno.exits` | 167 | not started |
| `baskfy_core.fno.gating` | 106 | not started |
| `baskfy_core.fno.journal` | 279 | not started |
| `baskfy_core.fno.ledger` | 347 | not started |
| `baskfy_core.fno.monitor` | 312 | not started |
| `baskfy_core.fno.research` | 1021 | not started |
| `baskfy_core.fno.retest` | 612 | not started |
| `baskfy_core.fno.scan` | 448 | not started |
| `baskfy_core.fno.series` | 217 | not started |
| `baskfy_core.fno.sizing` | 144 | not started |
| `baskfy_core.fno.underlying` | 123 | not started |
| `baskfy_core.fno.vol` | 187 | not started |
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
| `baskfy_core.ranking_engine` | 889 | goldens exist (L1/ranking), not ported |
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
| `baskfy_core.screener` | 1447 | goldens exist (L1/ranking), not ported |
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
| `baskfy_core.swing.plan` | 499 | ported — **15/16** (case_010 stale golden, R1.1) |
| `baskfy_core.swing.setups` | 425 | ported — 10/10 |
| `baskfy_core.swing.sizing` | 159 | ported — 11/11 |
| `baskfy_core.swing.stops` | 209 | ported — 16/16 |
| `baskfy_core.tenancy` | 57 | not started |
| `baskfy_core.tradebook` | 347 | not started |
| `baskfy_core.trading_calendar` | 185 | not started |
| `baskfy_core.twt` | 282 | not started |
| `baskfy_core.twt.backtest` | 756 | not started |
| `baskfy_core.twt.breadth` | 130 | not started |
| `baskfy_core.twt.calendar` | 95 | not started |
| `baskfy_core.twt.config` | 396 | not started |
| `baskfy_core.twt.drift` | 104 | not started |
| `baskfy_core.twt.exits` | 379 | not started |
| `baskfy_core.twt.indicators` | 184 | not started |
| `baskfy_core.twt.plan` | 550 | not started |
| `baskfy_core.twt.published` | 111 | not started |
| `baskfy_core.twt.signals` | 318 | not started |
| `baskfy_core.twt.sizing` | 184 | not started |
| `baskfy_core.twt.sleeve` | 148 | not started |
| `baskfy_core.universes` | 192 | not started |
| `baskfy_core.vbt` | 236 | not started |
| `baskfy_core.vbt.backtest` | 727 | not started |
| `baskfy_core.vbt.breadth` | 123 | not started |
| `baskfy_core.vbt.calendar` | 128 | not started |
| `baskfy_core.vbt.config` | 384 | not started |
| `baskfy_core.vbt.drift` | 78 | not started |
| `baskfy_core.vbt.exits` | 182 | not started |
| `baskfy_core.vbt.indicators` | 201 | not started |
| `baskfy_core.vbt.orders` | 196 | not started |
| `baskfy_core.vbt.plan` | 445 | not started |
| `baskfy_core.vbt.published` | 86 | not started |
| `baskfy_core.vbt.signals` | 138 | not started |
| `baskfy_core.vbt.sizing` | 161 | not started |
| `baskfy_core.vbt.sleeve` | 135 | not started |
| `baskfy_core.windows` | 211 | not started |

193 modules, 67550 lines in total. Goldens: 455 cases over 17 modules (swing 79, options 47,
backtest 139, ranking 190). Ported: 14 modules (11 with goldens + 3 configs/indicators), 125 of
126 of their goldens green. Verified: 10 of the 11 golden modules are fully green (all but
`swing.plan`); none is marked `verified` until the swing goldens are regenerated and reviewed.
