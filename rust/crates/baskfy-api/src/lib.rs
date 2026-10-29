//! Baskfy's HTTP API in Rust, ported from `decile-blueprint/services/api` (`baskfy_api`).
//!
//! The strangler rule applies (docs/rust-rewrite/README.md): Python serves until each route here
//! answers **exactly** as Python does — same status, same `problem+json`, same JSON bytes after
//! parsing (keys, key order, number and date spelling), same cache headers — graded by
//! `tools/parity/api_diff.py` against a copy of the database.
//!
//! The request pipeline, outermost first, is `app.py`'s:
//!
//! 1. [`cors`] — Starlette's `CORSMiddleware` (a preflight never reaches the app);
//! 2. the request context — `X-Request-Id` (honoured or minted), the access log line;
//! 3. [`http_cache`] — ETag / `Cache-Control` / `Vary` on the published analytics reads;
//! 4. routing on the **decoded** path, with Starlette's slash redirect and its 404/405;
//! 5. for `/api/v1/*` routes only, FastAPI's router dependency: the [`auth::Principal`]
//!    (bearer JWT, session epoch), then the [`ratelimit`];
//! 6. the handler; a [`problems::Problem`] it returns is rendered with the request path.
//!
//! **No route here may place, modify or cancel an order** (law 2; execution is the last wave) and
//! the public API stays shut (D9): `routes::public` is never mounted.

pub mod app;
pub mod auth;
pub mod body;
pub mod cache;
pub mod cors;
pub mod csrf;
pub mod email_address;
pub mod entitlements;
pub mod http_cache;
pub mod idempotency;
pub mod logging;
pub mod mailer;
pub mod problems;
pub mod query;
pub mod ratelimit;
pub mod routes;
pub mod security;
pub mod session;
pub mod settings;
pub mod state;
pub mod wire;

pub use app::build_app;
pub use settings::Settings;
pub use state::AppState;

/// A database failure as Python answers it: logged with its cause, answered with the 500
/// catch-all (`SQLAlchemyError` is not handled anywhere else in `app.py`).
#[must_use]
pub fn db_failure(e: &baskfy_db::DbError) -> problems::Problem {
    logging::error(
        "baskfy_api.app",
        "unhandled error",
        &[("error", &e.to_string())],
    );
    problems::internal_error()
}
