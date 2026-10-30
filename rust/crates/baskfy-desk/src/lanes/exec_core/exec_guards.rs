//! Lane **exec_guards** (child of `exec_core`): the layers below the gateway that refuse
//! before any network call — the instrument guards (SGB*/G-sec untouchable, the product
//! allow-list, the overnight-option and covered-carry rules), the account risk manager and its
//! Postgres ledger, the order-path rate limiter, the per-family read limiter, the IPv4 pin.
//!
//! **Python it ports:** `baskfy_execution/{guards,risk,ratelimit}.py` (re-exported by the desk's
//! `app/core/{guards,risk,ratelimit}.py`) and `app/core/{risk_store,kite_limits,net}.py`.
//! The code lives at `exec_core::{guards,risk,ratelimit,kite_limits,net}` because the gateway
//! lane wired those paths first; this lane owns their content and re-exports them here.
//! `app/core/websec.py`'s middleware is the foundation's [`crate::websec`].
//!
//! The non-negotiable tests for these modules are in `exec_guards/tests_*.rs`; they run with no
//! network and a recording fake broker.

pub use super::{guards, kite_limits, net, ratelimit, risk};

use axum::Router;

use crate::app::AppState;

/// The lane's routes: none (these layers have no HTTP surface of their own).
pub fn routes() -> Router<AppState> {
    Router::new()
}

#[cfg(test)]
mod tests_guards;
#[cfg(test)]
mod tests_limits;
#[cfg(test)]
mod tests_net_websec;
#[cfg(test)]
mod tests_risk;
