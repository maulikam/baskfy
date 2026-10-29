//! Baskfy's pure domain logic — the Rust port of `baskfy_core`
//! (`decile-blueprint/packages/core/src/baskfy_core`).
//!
//! **Law 1 (root `CLAUDE.md`), inherited verbatim:** this crate touches nothing. Frames and
//! values in, frames and values out. No database, no network, no disk, no clock, no
//! environment. `tests/law_one.rs` enforces it by scanning this crate's source and manifest.
//!
//! Every function is graded against the language-neutral goldens in `go/testdata/golden`
//! through `baskfy-testkit`; see `docs/rust-rewrite/README.md`.
//!
//! Module layout mirrors the Python package one-to-one (`baskfy_core.swing.stops` is
//! [`swing::stops`]). Porters add code inside the declared module files; new modules are
//! declared in the parent module file (`swing.rs`, `options.rs`), never by editing this file
//! for areas already declared.

pub mod backtest;
pub mod backtest_metrics;
pub mod error;
pub mod factor_registry;
pub mod factors;
pub mod fno;
pub mod options;
pub mod ranking_engine;
pub mod screener;
pub mod swing;
pub mod twt;
pub mod vbt;

pub use error::CoreError;
