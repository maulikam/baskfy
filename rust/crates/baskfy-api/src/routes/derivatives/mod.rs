//! The derivatives lane's private modules: what `routes::{options, fno, overlap, backtests}`
//! share, and the Python service modules they call (`options_read`, `fno_read`, `fno_settings`,
//! `options_settings`, `backtests`, `overlap`, `overlap_scan`, `curated_tenant`).
//!
//! Declared once, from `routes/options.rs` (`#[path]`), because a lane may not edit
//! `routes.rs`; the other three route files reach it as `super::options::derivatives`.

pub(crate) mod config_writes;
pub(crate) mod fno_read;
pub(crate) mod options_core;
pub(crate) mod options_read;
pub(crate) mod shared;
