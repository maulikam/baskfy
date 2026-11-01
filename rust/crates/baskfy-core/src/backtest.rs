//! Port of `baskfy_core.backtest`.
//!
//! the point-in-time backtest engine (`run_backtest`, `run_fragility`, schedules, weights, costs). Goldens: go/testdata/golden/L1/backtest.
//!
//! Split so two porters never share a file: [`helpers`] holds the schedules, weights, costs and
//! readers; [`engine`] holds `run_backtest` and `run_fragility`, which use them.

pub mod engine;
pub mod helpers;
