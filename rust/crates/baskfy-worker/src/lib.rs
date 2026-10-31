//! Baskfy's worker in Rust, ported from `decile-blueprint/services/worker` (`baskfy_worker`).
//!
//! Wave 5 ports the **nightly pipeline first** (docs/rust-rewrite/STATUS.md, "Wave 5"). The
//! strangler rule applies: Python's Celery task keeps running the chain until every step here
//! writes what Python writes, graded by `tools/parity/nightly_diff.py` on clones of the bed with
//! the replay provider serving identical inputs to both sides.
//!
//! | Python | Rust | state |
//! |---|---|---|
//! | `steps.py` | [`steps`] | ported (foundation) |
//! | `orchestrator.py` | [`orchestrator`] | ported (foundation); every step body is a lane's |
//! | `tasks/celery_tasks.nightly_pipeline` (explicit date) | [`nightly`] | ported (foundation) |
//! | `ops.begin_run`, `fail_run`, `is_trading_day` | [`ops`] | ported (foundation) |
//! | `calendar.classify`, `require_trading_day` | [`calendar`] | ported (foundation) |
//! | `alerts.Alert`, `dispatch` (log + email), `ops.alert_for_outcome` | [`alerts`] | ported (foundation); webhook and Sentry not |
//! | `settings.WorkerSettings` | [`settings`] | ported (foundation) |
//! | `deps.PipelineDependencies`, `window.DateWindow` | [`deps`], [`window`] | ported (foundation) |
//! | `catch_up.py` | [`catch_up`] | constants ported; the sweep is the gate lane's |
//! | every Celery task (`tasks/celery_tasks.py`, `tasks/desk.py`) and `BEAT_SCHEDULE` | [`tasks`] | registry and Beat table held equal to Python; four tasks wired (wave-5 code), the rest declared stubs per wave-6 lane |
//! | the step bodies (`tasks/*.py`) | [`steps::ingest`], [`steps::index`], [`steps::compute`], [`steps::gate`], [`steps::sleeves`] | **declared stubs**, one file per lane |
//!
//! **Law 2:** nothing in this crate places, modifies or cancels an order or a GTT, or calls a Kite
//! write endpoint. The worker reads market data and writes the analytics tables; execution is
//! the last wave. `tests/law_two.rs` scans the source.

pub mod alerts;
pub mod calendar;
pub mod catch_up;
pub mod deps;
pub mod error;
pub mod nightly;
pub mod ops;
pub mod orchestrator;
pub mod pyjson;
pub mod runtime;
pub mod settings;
pub mod steps;
pub mod tasks;
pub mod window;

pub use error::WorkerError;
