//! Lane **exec_kite** (child of `exec_core`): the desk's broker wrapper and its token at rest.
//!
//! **Python it ports:** `app/kite_client.py` (`Kite`: reads, each taking its family's slot; the
//! LTP cache; holdings = `quantity + t1_quantity + collateral_quantity` plus today's CNC buys,
//! pledged surfaced as `pledged_qty`, information only; and the **documented GTT exception** of
//! non-negotiable 6 — `place_gtt_stop` / `delete_gtt` carry their own untouchable and
//! overnight-option guards and are simulated under `DRY_RUN` before any broker call) and
//! `app/token_store.py` (the daily token Fernet-encrypted through the screener's
//! `AccessTokenStore`, the fallback key beside it at 0600).
//!
//! The code lives at `exec_core::{kite_client, token_store}` because the gateway lane wired
//! those paths first (`desk::DeskGateway::kite()`); this lane owns their content and
//! re-exports them here, as `exec_guards` does.
//!
//! **Not ported, deliberately:** `Kite.place_cnc_order`. Nothing in the desk calls it, and in
//! Rust it would be a second order path around the gateway (no risk, no rate limit, no
//! journal); the stricter boundary wins (law 2). The DRY_RUN check sits in front of every
//! broker write that is ported, exactly where Python has it.
//!
//! The tests are `tests/exec_kite.rs`: no network, a recording fake broker, and every DRY_RUN
//! path asserted never to reach it.

pub use super::{kite_client, token_store};

use axum::Router;

use crate::app::AppState;

/// The lane's routes: none (`/login` and `/callback` are the session lane's).
pub fn routes() -> Router<AppState> {
    Router::new()
}
