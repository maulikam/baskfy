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
//! **`kite()` is lazy** ([`kite`], [`kite_client::DeskKite::ready`]): `KITE_API_KEY missing`
//! raises from every method, the token is loaded and `FORCE_IPV4` applied on the first call.
//! `/status`'s session half is [`status_session`], for the session lane's route.
//!
//! The tests are `tests/exec_kite.rs` (no network, a recording fake broker, every DRY_RUN path
//! asserted never to reach it), `tests/exec_kite_goldens.rs` (175 broker-reply shapes against
//! the live Python's answers, `fixtures/exec_kite/goldens.json`) and
//! `tests/exec_kite_dotenv.rs` (the `.env`-only encryption key, in its own process).

pub use super::{kite_client, token_store};

use std::sync::Arc;

use axum::Router;

use self::kite_client::{ClientError, DeskKite};
use super::desk::DeskGateway;
use crate::app::AppState;
use crate::wire::{PyDict, PyValue, dict};

/// `app.main.kite()`: the process's one Kite wrapper, built (`Kite()`) on first call — so it
/// raises `RuntimeError("KITE_API_KEY missing — …")` without a key, every time, until one is
/// set. A route that Python writes as `k = kite()` starts here.
///
/// # Errors
/// `Kite()`'s refusal ([`DeskKite::ready`]), or a gateway that is not the desk's (no wrapper
/// to hand out — never a bare broker in its place).
pub fn kite(state: &AppState) -> Result<Arc<DeskKite>, ClientError> {
    let gateway = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<DeskGateway>())
        .ok_or_else(|| ClientError {
            python_type: "RuntimeError".to_owned(),
            message: "the desk's Kite wrapper is not wired (the gateway is not the desk's)"
                .to_owned(),
        })?;
    let k = gateway.kite();
    k.ready()?;
    Ok(k)
}

/// `/status`'s session half, exactly as `app.main.status` computes it:
///
/// ```python
/// try:
///     k = kite()
///     out |= {"authed": k.is_authed(),
///             "cash": k.available_cash() if k.is_authed() else None}
/// except Exception as exc:
///     out |= {"authed": False, "error": str(exc)}
/// ```
///
/// Through the wrapper, so each `is_authed` re-reads a rotated token and takes a `general` slot
/// before its `profile` — and `profile` is called twice, as Python calls it. The session lane's
/// `/status` merges this dict into its answer.
pub async fn status_session(state: &AppState) -> PyDict {
    let attempt = async {
        let k = kite(state)?;
        let authed = k.try_is_authed().await?;
        let cash = if k.try_is_authed().await? {
            PyValue::Float(k.available_cash().await?)
        } else {
            PyValue::None
        };
        Ok::<_, ClientError>(dict([("authed", PyValue::Bool(authed)), ("cash", cash)]))
    };
    match attempt.await {
        Ok(d) => d,
        Err(exc) => dict([
            ("authed", PyValue::Bool(false)),
            ("error", PyValue::Str(exc.message)),
        ]),
    }
}

/// The lane's routes: none (`/login` and `/callback` are the session lane's).
pub fn routes() -> Router<AppState> {
    Router::new()
}
