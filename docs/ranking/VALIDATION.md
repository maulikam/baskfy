# Ranking validation (C8) — evidence, promotion rule, statuses

Contract C8 in `docs/ranking/PLAN.md`; gate `gates/ranking-2.F-validation.md` (G4, G5). Decision
record: "Ranking 2.F" in `docs/DECISIONS-MERGE.md`.

**How this file is made.** The prose is written by hand. Every table between
`<!-- generated:… -->` markers is written by `research/ranking-validation/render_validation.py`
from `research/ranking-validation/out/ablation.csv`, and the promotion rule is applied by that
script, not by a reader. To refresh after a new run:

    cd decile-blueprint && uv run python ../research/ranking-validation/render_validation.py

Do not edit inside the markers; the next render overwrites it. The registry's `validation_status`
values, the preset statuses and the selection defaults are pinned to the generated tables by
`packages/core/tests/test_factor_registry_c1.py::test_validation_status_matches_validation_md`,
`packages/core/tests/test_ranking_presets.py::test_preset_statuses_match_validation_md` and
`packages/core/tests/test_ranking_selection.py::test_constraint_defaults_are_the_contracts`.

## 1. Method

The simulator is `baskfy_core.ranking_validation` (pure, law 1); the runner is
`research/ranking-validation/run_validation.py`. Factor values come from core's own
`compute_factors`, `factors_ranking` and the registry — no second formulas (gate G3). Ranks come
from core's `ranking_engine.rank_frame`, and holds, exits and entries from
`ranking_selection.select_portfolio`, the same selection rule the product uses.

- **Monthly rebalance, signal at close.** One decision per month-end session `d`. It reads only the
  factor rows dated `d` and the book as it stands; the spec tests edit a future price and show an
  earlier decision does not move.
- **Point-in-time universe.** EQ instruments with a bar on `d` whose trailing 63-session median
  traded value is at least ₹5 crore. Delisted instruments are in the export, so the history is
  survivorship-free.
- **Next-open fills.** The target set is fixed at the next session's open. Every target gets an
  equal slot of `1 / max_names` of equity marked at that open, and holds are resized back to their
  slot. Empty slots stay in cash earning nothing. An entrant with no open on the fill session is
  not bought.
- **Costs.** 25 bps a side on traded notional, buys and sells alike, taken from the book pro rata.
- **Buffer.** Top 20 names. A holding is kept while ranked inside `retention_rank`; a new name
  enters only inside `entry_rank`. Every model except the grid uses the incumbent 20/40.
- **Delisting rule.** A held name with no close is carried at its last close. After 5 consecutive
  sessions with no bar it is sold at that last close, less cost. The rule uses only the fact that
  today's bar is missing, never whether one arrives later.
- **Metrics.** One NAV path per model; IS and OOS are windows of it, not separate runs. IS ends at
  the last session before 2020-01-01, and OOS starts from that session's NAV. The metrics are net
  CAGR (calendar days / 365.25), max drawdown, annual one-way turnover, mean max-sector weight and
  per-year returns.
- **Models.**
  - `base`, plus base with one factor added for every rankable, non-legacy registry factor. Each
    added factor uses its registry preference, and `atr_ext_20` uses C7's target range `[0, 3]`.
  - Sortino vs Sharpe: the 12M+6M pair against the 12M+6M pair.
  - The desk's RSI penalty on and off.
  - Two regime eligibility filters.
  - The entry/retention grid.

## 2. Dataset

- Source: `research/volume-breakout/aws/`, the AWS export (`ohlcv_daily`, `instrument`,
  `index_member_daily`, `corporate_action`, `trading_day`, …). The export starts 2017-01-02.
- Signal dates: **102 month-ends, 2018-03-28 to 2026-08-31** (`out/full.log`). The first decision
  is March 2018 because the 12-month windows and `nse_mr12`'s 13-month anchor need a full year of
  bars. PLAN C8 asked for 2013-01, but the export does not go back that far.
- The simulation window runs to the last fill and mark after the final signal (2026-09-09). The
  cache holds 59,364 universe rows over 1,445 instruments, and the price panel is 2,090 sessions.
- One full run, 2026-09-13: 2,311 s, max RSS 1.88 GB (gate G2 evidence).

## 3. Known data limits — read before trusting a verdict

1. **"Base" is `avg_sharpe_12_6_3_1`, and that choice is the runner's.** PLAN C8 says "base model"
   but does not define one. The runner uses the screener's momentum blend, which is also the source
   of `mom_pctile`. Every factor verdict below is relative to that control. A different base could
   change them.
2. **NIFTY 500 levels are absent from the export.** `rs_persist_126` is NULL on every row, so its
   model is identical to `base`. It is **not testable**, not rejected, and stays `research`.
3. **`nse_momentum_score` has only partial inputs.** Nifty 200 / F&O membership is exported only
   from 2021-08-02. Before that date its model equals `base`: its IS window is identical to base,
   and so is its OOS max drawdown, which falls in 2020. The rule cannot judge it on the whole OOS
   window, so it is **not testable** and stays `research`. The rule's own pass on that row is shown
   but not used.
4. **The export has no sector membership.** Mean max-sector weight is empty for every model, and
   no sector cap was exercised. No verdict here says anything about sector concentration.
5. **Entry 25 is the same as entry 20.** With a 20-name book, the 25/30, 25/40 and 25/60 cells are
   identical, number for number, to 20/30, 20/40 and 20/60. Entry ranks beyond `max_names` add
   nothing here, and the grid table marks those rows.
6. **Base turnover is high: about 3.7 a year** (one-way, monthly rebalance at 25 bps a side). The
   turnover test is relative to that figure. A factor can pass while still trading a lot in
   absolute terms.
7. **The in-sample window is short.** IS covers 2018-03 to 2019-12, 21 months, against about 6.7
   years of OOS. The grid's IS stability test therefore guards against a cell that only won in one
   half, but it is weak evidence on its own.
8. **The grid used a 20-name book; the product's C5 default is 15 names.** Book size was not varied.
   The chosen cell's ranks are carried over as absolute ranks, on the view that a quality rank is a
   position in the universe, not a share of the book. That transfer is an assumption.
9. **One path, one export, no confidence intervals.** Several CAGR gaps below are a fraction of a
   point. The rule is mechanical so the statuses are reproducible, not because a 0.1-point gap is
   meaningful.

## 4. Metrics — every model

<!-- generated:metrics:begin (render_validation.py; do not edit) -->
Source: `research/ranking-validation/out/ablation.csv` (41 models, `ranking-validation-2.0.0`). Windows: full 2018-03-28 to 2026-09-09; IS 2018-03-28 to 2019-12-31; OOS 2019-12-31 to 2026-09-09 (OOS starts from IS's closing NAV). Every model: top 20, 25 bps a side. Drawdowns are peak-to-trough; turnover is one-way, per year. Max-sector weight is `n/a` for every model: the export has no sector membership.

| model | study | entry/ret | IS CAGR | OOS CAGR | OOS max DD | OOS turnover | full CAGR | full max DD | full turnover | max-sector wt |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `base` | base | 20/40 | -0.70% | 35.96% | -35.86% | 3.72 | 27.35% | -38.50% | 3.67 | n/a |
| `base+atr_ext_20` | factor | 20/40 | -8.45% | 40.25% | -39.31% | 7.12 | 28.33% | -40.59% | 6.85 | n/a |
| `base+ma50_slope_20` | factor | 20/40 | -2.15% | 35.63% | -36.33% | 5.30 | 26.71% | -43.04% | 5.14 | n/a |
| `base+eff_ratio_63` | factor | 20/40 | -3.12% | 34.84% | -33.76% | 6.21 | 25.86% | -39.98% | 6.04 | n/a |
| `base+max_dd_6m` | factor | 20/40 | 5.65% | 22.91% | -36.63% | 4.37 | 19.09% | -36.63% | 4.05 | n/a |
| `base+max_dd_12m` | factor | 20/40 | 6.77% | 25.67% | -33.40% | 3.55 | 21.48% | -33.40% | 3.31 | n/a |
| `base+downside_vol_6m` | factor | 20/40 | 8.78% | 19.50% | -33.62% | 3.59 | 17.18% | -33.62% | 3.31 | n/a |
| `base+downside_vol_12m` | factor | 20/40 | 12.16% | 19.45% | -29.84% | 3.06 | 17.90% | -29.84% | 2.84 | n/a |
| `base+sortino_6m` | factor | 20/40 | -0.52% | 34.02% | -34.73% | 4.23 | 25.95% | -34.73% | 4.07 | n/a |
| `base+sortino_12m` | factor | 20/40 | -2.57% | 34.47% | -37.46% | 3.33 | 25.74% | -38.21% | 3.28 | n/a |
| `base+underwater_12m` | factor | 20/40 | 4.46% | 34.08% | -36.26% | 2.97 | 27.29% | -36.26% | 2.86 | n/a |
| `base+ret_ex_top3_12m` | factor | 20/40 | -7.18% | 35.44% | -36.83% | 3.27 | 25.19% | -42.61% | 3.27 | n/a |
| `base+accel_21_105` | factor | 20/40 | -22.57% | 27.46% | -38.02% | 10.24 | 14.89% | -59.78% | 10.09 | n/a |
| `base+accel_21_105_vs` | factor | 20/40 | -14.31% | 25.38% | -35.25% | 10.34 | 15.82% | -50.10% | 10.08 | n/a |
| `base+vol_exp_21_126` | factor | 20/40 | 7.61% | 32.75% | -36.39% | 7.36 | 27.07% | -36.39% | 7.00 | n/a |
| `base+vol_persist_20` | factor | 20/40 | 4.76% | 32.60% | -37.02% | 6.80 | 26.25% | -37.02% | 6.49 | n/a |
| `base+resid_ret_12m` | factor | 20/40 | -7.16% | 32.69% | -39.88% | 3.39 | 23.18% | -43.48% | 3.33 | n/a |
| `base+rs_persist_126` | factor | 20/40 | -0.70% | 35.96% | -35.86% | 3.72 | 27.35% | -38.50% | 3.67 | n/a |
| `base+rank_persist_20` | factor | 20/40 | 0.50% | 36.86% | -34.42% | 3.44 | 28.34% | -34.42% | 3.37 | n/a |
| `base+regime_priority` | factor | 20/40 | -0.33% | 34.23% | -35.78% | 4.84 | 26.16% | -40.10% | 4.78 | n/a |
| `base+nse_momentum_score` | factor | 20/40 | -0.70% | 38.77% | -35.86% | 1.74 | 29.43% | -38.50% | 2.10 | n/a |
| `sharpe_12_6` | sortino | 20/40 | 4.16% | 35.74% | -33.80% | 3.72 | 28.46% | -33.80% | 3.56 | n/a |
| `sortino_for_sharpe` | sortino | 20/40 | 1.58% | 33.20% | -34.64% | 3.84 | 25.89% | -34.64% | 3.64 | n/a |
| `base+rsi_penalty` | rsi_penalty | 20/40 | -1.76% | 41.19% | -35.10% | 3.74 | 30.92% | -39.14% | 3.69 | n/a |
| `base|regime_in=BULL` | regime_filter | 20/40 | 0.52% | 35.41% | -35.78% | 4.85 | 27.26% | -40.10% | 4.81 | n/a |
| `base|regime_in=BULL+NEUTRAL` | regime_filter | 20/40 | 0.07% | 39.21% | -39.16% | 4.44 | 29.96% | -39.25% | 4.45 | n/a |
| `grid_e10_r20` | grid | 10/20 | -5.61% | 23.60% | -24.37% | 2.87 | 16.85% | -29.58% | 2.87 | n/a |
| `grid_e10_r30` | grid | 10/30 | -7.77% | 26.36% | -30.50% | 2.81 | 18.34% | -37.32% | 2.81 | n/a |
| `grid_e10_r40` | grid | 10/40 | -3.28% | 28.79% | -35.90% | 2.78 | 21.33% | -36.80% | 2.73 | n/a |
| `grid_e10_r60` | grid | 10/60 | -0.40% | 36.93% | -34.41% | 2.62 | 28.14% | -34.41% | 2.55 | n/a |
| `grid_e15_r20` | grid | 15/20 | -7.06% | 31.60% | -36.61% | 4.27 | 22.41% | -36.61% | 4.30 | n/a |
| `grid_e15_r30` | grid | 15/30 | -9.69% | 35.36% | -35.45% | 4.01 | 24.42% | -43.64% | 4.01 | n/a |
| `grid_e15_r40` | grid | 15/40 | -2.25% | 35.87% | -36.84% | 3.57 | 26.87% | -37.61% | 3.52 | n/a |
| `grid_e15_r60` | grid | 15/60 | -0.55% | 39.43% | -34.41% | 3.05 | 29.95% | -34.41% | 2.96 | n/a |
| `grid_e20_r20` | grid | 20/20 | -6.85% | 40.24% | -32.79% | 5.68 | 28.78% | -40.54% | 5.72 | n/a |
| `grid_e20_r30` | grid | 20/30 | -8.12% | 37.50% | -34.92% | 4.42 | 26.42% | -44.15% | 4.43 | n/a |
| `grid_e20_r40` | grid | 20/40 | -0.70% | 35.96% | -35.86% | 3.72 | 27.35% | -38.50% | 3.67 | n/a |
| `grid_e20_r60` | grid | 20/60 | 1.93% | 39.32% | -34.19% | 3.09 | 30.54% | -34.19% | 3.01 | n/a |
| `grid_e25_r30` | grid | 25/30 | -8.12% | 37.50% | -34.92% | 4.42 | 26.42% | -44.15% | 4.43 | n/a |
| `grid_e25_r40` | grid | 25/40 | -0.70% | 35.96% | -35.86% | 3.72 | 27.35% | -38.50% | 3.67 | n/a |
| `grid_e25_r60` | grid | 25/60 | 1.93% | 39.32% | -34.19% | 3.09 | 30.54% | -34.19% | 3.01 | n/a |

**Per-year net return** (2018 starts at the first fill after 2018-03-28; 2026 ends 2026-09-09.)

| model | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `base` | -11.02% | 11.01% | 64.17% | 102.17% | 4.07% | 61.79% | 44.66% | -16.10% | 15.18% |
| `base+atr_ext_20` | -17.03% | 3.16% | 61.06% | 117.67% | 10.95% | 84.73% | 53.22% | -23.55% | 14.26% |
| `base+ma50_slope_20` | -6.28% | 2.69% | 66.06% | 83.41% | 8.36% | 81.93% | 28.75% | -16.14% | 18.56% |
| `base+eff_ratio_63` | -12.85% | 8.51% | 55.42% | 74.09% | 13.60% | 69.69% | 37.79% | -16.47% | 23.10% |
| `base+max_dd_6m` | -2.34% | 12.79% | 23.57% | 72.27% | 4.75% | 42.85% | 31.30% | -5.01% | 0.07% |
| `base+max_dd_12m` | 2.49% | 9.50% | 23.84% | 50.34% | 5.61% | 61.06% | 31.04% | 0.13% | 11.04% |
| `base+downside_vol_6m` | 5.98% | 9.42% | 26.01% | 46.60% | 12.40% | 36.66% | 28.84% | -5.31% | -4.85% |
| `base+downside_vol_12m` | 9.13% | 12.15% | 25.50% | 30.74% | 12.30% | 45.57% | 27.23% | -1.99% | -1.79% |
| `base+sortino_6m` | -10.09% | 10.21% | 87.13% | 75.49% | 2.83% | 52.90% | 39.96% | -14.92% | 15.39% |
| `base+sortino_12m` | -13.97% | 11.02% | 48.07% | 105.77% | -0.08% | 73.95% | 37.52% | -11.13% | 12.14% |
| `base+underwater_12m` | -5.62% | 14.42% | 55.50% | 104.96% | 2.08% | 74.80% | 31.26% | -13.54% | 10.27% |
| `base+ret_ex_top3_12m` | -19.86% | 9.44% | 62.03% | 91.67% | -1.36% | 73.28% | 46.98% | -14.81% | 14.53% |
| `base+accel_21_105` | -28.13% | -11.31% | 28.08% | 83.16% | 10.38% | 51.50% | 33.09% | -12.82% | 11.41% |
| `base+accel_21_105_vs` | -19.07% | -5.84% | 32.07% | 85.45% | 9.42% | 41.02% | 27.01% | -12.59% | 8.23% |
| `base+vol_exp_21_126` | -1.85% | 15.94% | 55.54% | 58.08% | 34.37% | 67.39% | 24.62% | -18.62% | 18.71% |
| `base+vol_persist_20` | -3.47% | 12.42% | 51.03% | 76.46% | 22.07% | 80.96% | 18.38% | -21.34% | 20.54% |
| `base+resid_ret_12m` | -21.15% | 11.28% | 56.05% | 95.69% | 2.18% | 67.69% | 40.73% | -20.56% | 13.47% |
| `base+rs_persist_126` | -11.02% | 11.01% | 64.17% | 102.17% | 4.07% | 61.79% | 44.66% | -16.10% | 15.18% |
| `base+rank_persist_20` | -6.23% | 7.58% | 78.58% | 127.68% | -4.61% | 57.89% | 39.92% | -19.35% | 18.15% |
| `base+regime_priority` | -4.71% | 4.33% | 65.32% | 104.43% | -1.82% | 63.82% | 49.46% | -21.37% | 12.26% |
| `base+nse_momentum_score` | -11.02% | 11.01% | 64.17% | 60.27% | 21.65% | 56.20% | 45.18% | 17.10% | 5.40% |
| `sharpe_12_6` | -4.85% | 12.93% | 71.44% | 87.83% | 3.66% | 61.89% | 45.32% | -17.77% | 19.67% |
| `sortino_for_sharpe` | -7.84% | 11.55% | 63.49% | 98.98% | -2.43% | 54.94% | 45.38% | -15.40% | 12.59% |
| `base+rsi_penalty` | -12.65% | 10.96% | 62.63% | 116.26% | 8.61% | 72.22% | 52.73% | -14.24% | 16.72% |
| `base|regime_in=BULL` | -2.27% | 3.27% | 69.61% | 104.43% | -1.82% | 69.26% | 49.46% | -21.37% | 12.26% |
| `base|regime_in=BULL+NEUTRAL` | -0.64% | 0.77% | 72.82% | 118.47% | 3.16% | 73.54% | 57.14% | -22.29% | 10.82% |
| `grid_e10_r20` | -9.76% | 0.10% | 60.40% | 61.91% | -4.63% | 34.01% | 26.79% | -9.06% | 7.86% |
| `grid_e10_r30` | -13.71% | 0.51% | 53.17% | 76.20% | -3.51% | 42.52% | 29.72% | -8.93% | 9.16% |
| `grid_e10_r40` | -14.02% | 9.67% | 53.02% | 86.03% | -2.34% | 51.65% | 28.27% | -6.13% | 7.08% |
| `grid_e10_r60` | -15.24% | 17.14% | 74.72% | 109.59% | 1.66% | 61.44% | 28.74% | -3.88% | 10.12% |
| `grid_e15_r20` | -12.80% | 0.82% | 67.92% | 103.91% | -0.73% | 49.14% | 30.06% | -19.71% | 18.66% |
| `grid_e15_r30` | -18.01% | 1.94% | 65.62% | 107.91% | 1.00% | 64.10% | 37.53% | -18.66% | 18.80% |
| `grid_e15_r40` | -14.04% | 11.76% | 62.14% | 115.40% | 1.80% | 68.22% | 34.50% | -16.36% | 15.59% |
| `grid_e15_r60` | -15.34% | 16.97% | 77.04% | 113.67% | 3.58% | 64.28% | 39.64% | -14.35% | 20.09% |
| `grid_e20_r20` | -14.89% | 3.68% | 80.95% | 117.77% | 2.92% | 57.77% | 47.62% | -16.73% | 22.19% |
| `grid_e20_r30` | -15.22% | 1.62% | 69.78% | 106.95% | 6.25% | 60.65% | 47.02% | -18.60% | 17.33% |
| `grid_e20_r40` | -11.02% | 11.01% | 64.17% | 102.17% | 4.07% | 61.79% | 44.66% | -16.10% | 15.18% |
| `grid_e20_r60` | -11.91% | 17.41% | 73.06% | 113.50% | 3.58% | 62.86% | 44.82% | -15.17% | 20.09% |
| `grid_e25_r30` | -15.22% | 1.62% | 69.78% | 106.95% | 6.25% | 60.65% | 47.02% | -18.60% | 17.33% |
| `grid_e25_r40` | -11.02% | 11.01% | 64.17% | 102.17% | 4.07% | 61.79% | 44.66% | -16.10% | 15.18% |
| `grid_e25_r60` | -11.91% | 17.41% | 73.06% | 113.50% | 3.58% | 62.86% | 44.82% | -15.17% | 20.09% |
<!-- generated:metrics:end -->

## 5. Promotion rule and factor statuses

**Rule** (the gate's rule; PLAN C8 states none of its own). A candidate is tested against its
control on the **out-of-sample** window and passes only if all three hold:

1. OOS net CAGR ≥ the control's;
2. OOS max drawdown is not worse than the control's by more than 2 percentage points;
3. OOS annual turnover is not more than 25% above the control's.

**Testability, checked first.** A factor is *not testable* when the run could not measure it: its
coverage in the factor cache is 0, or the runner recorded a data limit for it (`note`). Outcomes
map to the registry as follows: pass → `validated`, fail → `rejected`, not testable → `research`
(unchanged). C1 factors that are not rankable (`atr_14`, `excess_ret_*`, `mom_pctile`, `nse_mr6`,
`nse_mr12`) were not modelled and stay `research`.

<!-- generated:promotion:begin (render_validation.py; do not edit) -->
Control: `base` — OOS CAGR 35.96%, max DD -35.86%, turnover 3.72. Thresholds: Δ CAGR ≥ 0; Δ max DD ≥ -2 pts; turnover ≤ 1.25 x base (4.65).

| factor | coverage | OOS CAGR | Δ CAGR pts | Δ max DD pts | turnover x base | CAGR | DD | turnover | status | registry |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |
| `atr_ext_20` | 100.00% | 40.25% | +4.29 | -3.45 | 1.91 | pass | FAIL | FAIL | **rejected** | rejected |
| `ma50_slope_20` | 99.73% | 35.63% | -0.33 | -0.47 | 1.42 | FAIL | pass | FAIL | **rejected** | rejected |
| `eff_ratio_63` | 99.94% | 34.84% | -1.13 | +2.10 | 1.67 | FAIL | pass | FAIL | **rejected** | rejected |
| `max_dd_6m` | 98.19% | 22.91% | -13.05 | -0.77 | 1.17 | FAIL | pass | pass | **rejected** | rejected |
| `max_dd_12m` | 95.10% | 25.67% | -10.29 | +2.46 | 0.95 | FAIL | pass | pass | **rejected** | rejected |
| `downside_vol_6m` | 98.18% | 19.50% | -16.46 | +2.24 | 0.97 | FAIL | pass | pass | **rejected** | rejected |
| `downside_vol_12m` | 95.08% | 19.45% | -16.51 | +6.02 | 0.82 | FAIL | pass | pass | **rejected** | rejected |
| `sortino_6m` | 98.18% | 34.02% | -1.94 | +1.13 | 1.14 | FAIL | pass | pass | **rejected** | rejected |
| `sortino_12m` | 95.08% | 34.47% | -1.49 | -1.60 | 0.90 | FAIL | pass | pass | **rejected** | rejected |
| `underwater_12m` | 95.10% | 34.08% | -1.88 | -0.40 | 0.80 | FAIL | pass | pass | **rejected** | rejected |
| `ret_ex_top3_12m` | 95.10% | 35.44% | -0.52 | -0.97 | 0.88 | FAIL | pass | pass | **rejected** | rejected |
| `accel_21_105` | 98.10% | 27.46% | -8.50 | -2.16 | 2.75 | FAIL | FAIL | FAIL | **rejected** | rejected |
| `accel_21_105_vs` | 98.10% | 25.38% | -10.59 | +0.61 | 2.78 | FAIL | pass | FAIL | **rejected** | rejected |
| `vol_exp_21_126` | 97.57% | 32.75% | -3.21 | -0.53 | 1.98 | FAIL | pass | FAIL | **rejected** | rejected |
| `vol_persist_20` | 97.59% | 32.60% | -3.36 | -1.16 | 1.83 | FAIL | pass | FAIL | **rejected** | rejected |
| `resid_ret_12m` | 93.53% | 32.69% | -3.27 | -4.02 | 0.91 | FAIL | FAIL | pass | **rejected** | rejected |
| `rs_persist_126` | 0.00% | 35.96% | +0.00 | +0.00 | 1.00 | pass | pass | pass | **not testable** | research |
| `rank_persist_20` | 90.34% | 36.86% | +0.90 | +1.44 | 0.93 | pass | pass | pass | **validated** | validated |
| `regime_priority` | 100.00% | 34.23% | -1.73 | +0.08 | 1.30 | FAIL | pass | FAIL | **rejected** | rejected |
| `nse_momentum_score` | n/a | 38.77% | +2.81 | +0.00 | 0.47 | pass | pass | pass | **not testable** | research |

Outcome: 1 validated (`rank_persist_20`), 17 rejected, 2 not testable.

- `rs_persist_126` — not testable: NIFTY 500 levels absent from the export: factor is NULL, row equals base. The rule's own result on this row was pass; it is not used.
- `nse_momentum_score` — not testable: Nifty 200 / F&O membership exported only from 2021-08-02. The rule's own result on this row was pass; it is not used.
<!-- generated:promotion:end -->

**Extra studies** — the same rule against each study's control. These verdicts inform the preset
work only. None of them is a registry factor, so none moves a `validation_status`. Sortino has its
own `sortino_6m` / `sortino_12m` rows above; PLAN correction 7 requires it to add information over
Sharpe in a hold-out, and this row tests exactly that.

<!-- generated:studies:begin (render_validation.py; do not edit) -->
| study model | control | Δ CAGR pts | Δ max DD pts | turnover x control | CAGR | DD | turnover | rule |
| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |
| `sortino_for_sharpe` | `sharpe_12_6` | -2.54 | -0.85 | 1.03 | FAIL | pass | pass | **fail** |
| `base+rsi_penalty` | `base` | +5.23 | +0.76 | 1.00 | pass | pass | pass | **pass** |
| `base|regime_in=BULL` | `base` | -0.55 | +0.08 | 1.30 | FAIL | pass | FAIL | **fail** |
| `base|regime_in=BULL+NEUTRAL` | `base` | +3.25 | -3.30 | 1.19 | pass | FAIL | pass | **fail** |
<!-- generated:studies:end -->

## 6. Entry/retention grid and the selection default

PLAN C8 asks for "the grid's best OOS-stable cell". Here, a cell is *stable* when it passes the
promotion rule against the incumbent 20/40 cell in **both** the IS and the OOS windows. The best
stable cell is the one with the highest OOS net CAGR; ties go to lower OOS turnover, then the
narrower retention. That cell sets `SelectionConstraints.entry_rank` / `retention_rank` in
`baskfy_core.ranking_selection`, mirrored by the API's `SelectionConstraintsIn`. `max_names` stays
at C5's 15 (limit 8).

`ValidationConfig`'s 20/40 is **not** moved. It is the incumbent this evidence was measured
against, and `ablation.csv` is only reproducible with it.

<!-- generated:grid:begin (render_validation.py; do not edit) -->
| entry/retention | IS CAGR | OOS CAGR | OOS max DD | OOS turnover | rule vs 20/40, IS | rule vs 20/40, OOS | verdict | identical to |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| 10/20 | -5.61% | 23.60% | -24.37% | 2.87 | fail | fail | not stable |  |
| 10/30 | -7.77% | 26.36% | -30.50% | 2.81 | fail | fail | not stable |  |
| 10/40 | -3.28% | 28.79% | -35.90% | 2.78 | fail | fail | not stable |  |
| 10/60 | -0.40% | 36.93% | -34.41% | 2.62 | pass | pass | stable |  |
| 15/20 | -7.06% | 31.60% | -36.61% | 4.27 | fail | fail | not stable |  |
| 15/30 | -9.69% | 35.36% | -35.45% | 4.01 | fail | fail | not stable |  |
| 15/40 | -2.25% | 35.87% | -36.84% | 3.57 | fail | fail | not stable |  |
| 15/60 | -0.55% | 39.43% | -34.41% | 3.05 | pass | pass | stable |  |
| 20/20 | -6.85% | 40.24% | -32.79% | 5.68 | fail | fail | not stable |  |
| 20/30 | -8.12% | 37.50% | -34.92% | 4.42 | fail | pass | not stable |  |
| 20/40 | -0.70% | 35.96% | -35.86% | 3.72 | pass | pass | incumbent |  |
| 20/60 | 1.93% | 39.32% | -34.19% | 3.09 | pass | pass | stable |  |
| 25/30 | -8.12% | 37.50% | -34.92% | 4.42 | fail | pass | duplicate | ≡ e20/r30 |
| 25/40 | -0.70% | 35.96% | -35.86% | 3.72 | pass | pass | duplicate | ≡ e20/r40 |
| 25/60 | 1.93% | 39.32% | -34.19% | 3.09 | pass | pass | duplicate | ≡ e20/r60 |

Stable cells, best first: 15/60, 20/60, 10/60. **Best OOS-stable cell: 15/60.**

Selection default (`SelectionConstraints`, C5): `entry_rank=15, retention_rank=60`.
<!-- generated:grid:end -->

## 7. Preset statuses

The presets are PLAN C7's seven (`baskfy_core.ranking_presets.PRESET_SPECS`). A preset is `ready`
only when **every** factor it ranks by was `validated` here. One term that was `rejected` or
`not testable` makes it `research`. A preset none of whose factors was modelled keeps its declared
status: that is only `desk_quality`, which stays `ready` on its parity with `baskfy_core.score`,
and that parity is not C8 evidence. `pos_days_6m`, `ma_stack_score`, `avg_sharpe_12_6_3_1` and
`desk_score` are outside the C1 ablation ("not in C8").

The outcome: only `desk_quality` is `ready`. Five of the six C7 composites or tie-break orders use a
factor C8 rejected. `leadership` pairs the one validated factor, `rank_persist_20`, with
`rs_persist_126`, which was not testable. `nse_momentum` is not testable (§3.3). `desk_sequential`
ranks by `desk_score` then `atr_ext_20`: C7's third tie-break, `median_vol_12m`, is a result column,
not a registry factor, so no ranking term can carry it (DECISIONS-MERGE 2D.8).

<!-- generated:presets:begin (render_validation.py; do not edit) -->
| preset | mode | terms | C8 evidence | status |
| --- | --- | --- | --- | --- |
| `desk_quality` | single | `desk_score` | not in C8: `desk_score` | ready |
| `path_quality` | composite | `pos_days_6m`, `max_dd_12m`, `downside_vol_12m`, `ret_ex_top3_12m` | not in C8: `pos_days_6m`; rejected: `max_dd_12m`, `downside_vol_12m`, `ret_ex_top3_12m` | research |
| `trend_structure` | composite | `ma_stack_score`, `ma50_slope_20`, `atr_ext_20` | not in C8: `ma_stack_score`; rejected: `ma50_slope_20`, `atr_ext_20` | research |
| `participation` | composite | `vol_exp_21_126`, `vol_persist_20` | rejected: `vol_exp_21_126`, `vol_persist_20` | research |
| `leadership` | composite | `rank_persist_20`, `rs_persist_126`, `avg_sharpe_12_6_3_1` | validated: `rank_persist_20`; not testable: `rs_persist_126`; not in C8: `avg_sharpe_12_6_3_1` | research |
| `nse_momentum` | single | `nse_momentum_score` | not testable: `nse_momentum_score` | research |
| `desk_sequential` | sequential | `desk_score` → `atr_ext_20` | not in C8: `desk_score`; rejected: `atr_ext_20` | research |
<!-- generated:presets:end -->
