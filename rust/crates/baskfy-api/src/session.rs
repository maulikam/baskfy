//! The write routes' database session: Python's `get_session` (`baskfy_api.db`), and the one
//! door to the read-write pool (DECISIONS-RS R6.2).
//!
//! **Who can write.** [`AppState`](crate::state::AppState) holds the [`WriteDb`] in a private
//! field. The only code that reads it is [`write_access`], the route layer `app.rs` installs on
//! `/api/v1`, and it hands a [`WritePool`] to the request **only when the method is POST, PUT,
//! PATCH or DELETE**. A GET (or HEAD/OPTIONS) handler that asks for `Extension<WritePool>` gets
//! axum's missing-extension 500 — `tests` below prove it, and `tests/write_access.rs` proves no
//! source file outside this module and `app.rs` names the field. GET routes keep the read-only
//! `state.db`.
//!
//! **The transaction.** [`run`] is `get_session`: one transaction per request, committed only if
//! the handler returns `Ok`, rolled back on any `Err` (a `Problem` is an exception in Python, and
//! it unwinds through the session dependency, which rolls back). That includes the paths that
//! *flush* before they raise — `rotate_refresh`'s reuse revocation and expiry stamp are rolled
//! back with the 401, exactly as Python's are (R6.4).

use std::future::Future;
use std::pin::Pin;

use axum::extract::{Request, State};
use axum::http::Method;
use axum::middleware::Next;
use axum::response::Response;
use baskfy_db::WriteDb;
use sqlx::{Postgres, Transaction};

use crate::problems::Problem;
use crate::state::AppState;

/// The request's transaction on the write pool.
pub type Tx = Transaction<'static, Postgres>;

/// The write pool, as a write request receives it (a request extension).
#[derive(Debug, Clone)]
pub struct WritePool(WriteDb);

/// Whether `method` may carry a write: Starlette's mutating verbs.
#[must_use]
pub fn is_write_method(method: &Method) -> bool {
    matches!(
        *method,
        Method::POST | Method::PUT | Method::PATCH | Method::DELETE
    )
}

/// The route layer on `/api/v1`: a write request gets the [`WritePool`]; nothing else does.
pub async fn write_access(
    State(state): State<AppState>,
    mut request: Request,
    next: Next,
) -> Response {
    if is_write_method(request.method())
        && let Some(write) = state.write_db()
    {
        request.extensions_mut().insert(WritePool(write.clone()));
    }
    next.run(request).await
}

/// A future borrowing the transaction (the shape [`run`]'s body returns).
pub type TxFuture<'t, T> = Pin<Box<dyn Future<Output = Result<T, Problem>> + Send + 't>>;

/// `get_session`: begin, run `body`, commit on `Ok`, roll back on `Err`.
///
/// # Errors
/// `body`'s problem; the 500 catch-all when no connection can be had or the commit fails.
pub async fn run<T, F>(pool: &WritePool, body: F) -> Result<T, Problem>
where
    F: for<'t> FnOnce(&'t mut Tx) -> TxFuture<'t, T>,
{
    let mut tx = pool.0.begin().await.map_err(|e| crate::db_failure(&e))?;
    match body(&mut tx).await {
        Ok(value) => {
            tx.commit()
                .await
                .map_err(|e| crate::db_failure(&e.into()))?;
            Ok(value)
        }
        // Dropping the transaction rolls it back (sqlx issues the ROLLBACK on drop).
        Err(problem) => Err(problem),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use axum::Router;
    use axum::body::Body;
    use axum::extract::Extension;
    use axum::middleware::from_fn_with_state;
    use axum::routing::{get, post};
    use tower::ServiceExt;

    use crate::settings::Settings;

    fn state() -> AppState {
        let settings = Settings {
            database_url: baskfy_providers::secret::Secret::new("postgresql://u:p@127.0.0.1:9/d"),
            rate_limit_enabled: false,
            ..Settings::default()
        };
        AppState::from_settings(settings).unwrap()
    }

    async fn status(app: Router, method: Method) -> u16 {
        let request = axum::http::Request::builder()
            .method(method)
            .uri("/probe")
            .body(Body::empty())
            .unwrap();
        app.oneshot(request).await.unwrap().status().as_u16()
    }

    #[tokio::test]
    async fn a_get_handler_cannot_obtain_the_write_pool() {
        let state = state();
        assert!(
            state.write_db().is_some(),
            "the state does hold a write pool"
        );
        // A GET handler that asks for the pool: the layer never inserts it, so the extractor
        // refuses (axum's missing-extension 500) and the handler body never runs.
        async fn reads(Extension(_pool): Extension<WritePool>) -> &'static str {
            "unreachable"
        }
        let app = Router::new()
            .route("/probe", get(reads).post(reads).delete(reads))
            .route_layer(from_fn_with_state(state.clone(), write_access))
            .with_state(state);
        assert_eq!(status(app.clone(), Method::GET).await, 500);
        assert_eq!(status(app.clone(), Method::POST).await, 200);
        assert_eq!(status(app, Method::DELETE).await, 200);
    }

    #[tokio::test]
    async fn only_mutating_methods_count_as_writes() {
        for m in [Method::POST, Method::PUT, Method::PATCH, Method::DELETE] {
            assert!(is_write_method(&m), "{m}");
        }
        for m in [Method::GET, Method::HEAD, Method::OPTIONS, Method::TRACE] {
            assert!(!is_write_method(&m), "{m}");
        }
        // And a POST with no write pool configured (the database-free test state) gets none.
        let mut bare = state();
        bare.clear_write_db();
        async fn writes(pool: Option<Extension<WritePool>>) -> &'static str {
            if pool.is_some() { "pool" } else { "none" }
        }
        let app = Router::new()
            .route("/probe", post(writes))
            .route_layer(from_fn_with_state(bare.clone(), write_access))
            .with_state(bare);
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .method(Method::POST)
                    .uri("/probe")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = axum::body::to_bytes(response.into_body(), 64)
            .await
            .unwrap();
        assert_eq!(&bytes[..], b"none");
    }
}
